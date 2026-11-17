"""Oracle Cloud Always Free instance launcher.

Keeps calling launch_instance while OCI answers "Out of host capacity" and tells you
when it finally gets the instance.

Based on mohankumarpaluru/oracle-freetier-instance-creation (MIT, (c) 2023 Mohan Kumar Paluru).

Usage:
    python main.py            # launch (default): retry until the instance exists
    python main.py check      # read-only: show what would be launched and the Always Free check
    python main.py resize     # grow an existing A1.Flex to RESIZE_OCPUS / RESIZE_MEMORY_GB
"""
import argparse
import configparser
import itertools
import json
import logging
import os
import shutil
import smtplib
import subprocess
import sys
import threading
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from types import SimpleNamespace

import oci
import requests
from dotenv import load_dotenv

ARM_SHAPE = "VM.Standard.A1.Flex"
E2_MICRO_SHAPE = "VM.Standard.E2.1.Micro"

# Always Free limits per tenancy (https://docs.oracle.com/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
FREE_A1_OCPUS = 4
FREE_A1_MEMORY_GB = 24
FREE_MICRO_INSTANCES = 2
FREE_STORAGE_GB = 200  # boot + block volumes together
MIN_BOOT_VOLUME_GB = 50
GONE = ("TERMINATED", "TERMINATING")

WAIT_TIME = 60  # seconds between retries; overwritten from REQUEST_WAIT_TIME_SECS in main()

log = logging.getLogger("launch_instance")

IMAGE_LIST_KEYS = ["lifecycle_state", "display_name", "id", "operating_system",
                   "operating_system_version", "size_in_mbs", "time_created"]


class ConfigError(Exception):
    """Bad or missing configuration: the user has to fix a file, retrying will not help."""


# --------------------------------------------------------------------------- settings

def _bool(value):
    return str(value).strip().lower() in ("true", "1", "y", "yes")


def load_settings(env):
    """Build the settings from a mapping (os.environ after loading oci.env).

    Values may contain spaces (e.g. Windows paths); surrounding quotes are handled by python-dotenv.
    """
    def get(key, default=""):
        return (env.get(key) or default).strip()

    shape = get("OCI_COMPUTE_SHAPE", ARM_SHAPE)
    arm = shape == ARM_SHAPE
    try:
        s = SimpleNamespace(
            oci_config=get("OCI_CONFIG", "~/.oci/config"),
            ad_suffixes=[a.strip() for a in get("OCT_FREE_AD", "AD-1").split(",") if a.strip()],
            display_name=get("DISPLAY_NAME", "free-instance"),
            wait_secs=int(get("REQUEST_WAIT_TIME_SECS", "60")),
            ssh_key_file=get("SSH_AUTHORIZED_KEYS_FILE", "id_rsa.pub"),
            image_id=get("OCI_IMAGE_ID") or None,
            shape=shape,
            second_micro=_bool(get("SECOND_MICRO_INSTANCE", "false")),
            subnet_id=get("OCI_SUBNET_ID") or None,
            operating_system=get("OPERATING_SYSTEM", "Canonical Ubuntu"),
            os_version=get("OS_VERSION", "22.04"),
            assign_public_ip=_bool(get("ASSIGN_PUBLIC_IP", "true")),
            boot_volume_gb=int(get("BOOT_VOLUME_SIZE", "50")),
            ocpus=float(get("OCPUS", "2" if arm else "1")),
            memory_gb=float(get("MEMORY_GB", "12" if arm else "1")),
            resize_ocpus=float(get("RESIZE_OCPUS", str(FREE_A1_OCPUS))),
            resize_memory_gb=float(get("RESIZE_MEMORY_GB", str(FREE_A1_MEMORY_GB))),
            create_network=_bool(get("CREATE_NETWORK", "true")),
            desktop_notify=_bool(get("DESKTOP_NOTIFY", "true")),
            notify_email=_bool(get("NOTIFY_EMAIL", "false")),
            email=get("EMAIL"),
            email_password=get("EMAIL_PASSWORD"),
            discord_webhook=get("DISCORD_WEBHOOK"),
            telegram_token=get("TELEGRAM_TOKEN"),
            telegram_user_id=get("TELEGRAM_USER_ID"),
        )
    except ValueError as err:
        raise ConfigError(f"oci.env has a value that is not a number: {err}") from err
    if s.wait_secs < 1:  # 0 would hammer the API and get the account throttled
        raise ConfigError("REQUEST_WAIT_TIME_SECS must be at least 1 (60 recommended).")
    return s


# --------------------------------------------------------------------------- Always Free check

def _alive(instances, exclude_id=None):
    return [i for i in instances if i.lifecycle_state not in GONE and i.id != exclude_id]


def free_tier_problems(shape, ocpus, memory_gb, new_disk_gb, instances, storage_used_gb,
                       replacing_id=None):
    """Return the reasons why this request would leave Always Free (empty list = it fits).

    instances: existing instances in the tenancy. replacing_id: instance being resized,
    so its current size is not counted twice.
    """
    problems = []
    alive = _alive(instances, replacing_id)
    if shape == ARM_SHAPE:
        arm = [i for i in alive if i.shape == ARM_SHAPE]
        used_ocpus = sum(i.shape_config.ocpus for i in arm)
        used_mem = sum(i.shape_config.memory_in_gbs for i in arm)
        if ocpus <= 0 or memory_gb <= 0:
            problems.append("OCPUS and MEMORY_GB must be greater than 0.")
        if used_ocpus + ocpus > FREE_A1_OCPUS:
            problems.append(f"A1 OCPUs: {used_ocpus:g} in use + {ocpus:g} requested "
                            f"> {FREE_A1_OCPUS} free.")
        if used_mem + memory_gb > FREE_A1_MEMORY_GB:
            problems.append(f"A1 memory: {used_mem:g} GB in use + {memory_gb:g} GB requested "
                            f"> {FREE_A1_MEMORY_GB} GB free.")
        if ocpus > 0 and not ocpus <= memory_gb <= 64 * ocpus:
            problems.append("A1.Flex needs between 1 and 64 GB of memory per OCPU.")
    elif shape == E2_MICRO_SHAPE:
        micros = sum(1 for i in alive if i.shape == E2_MICRO_SHAPE)
        if micros >= FREE_MICRO_INSTANCES:
            problems.append(f"There are already {micros} E2.1.Micro instances "
                            f"(max {FREE_MICRO_INSTANCES} free).")
    else:
        problems.append(f"{shape} is not an Always Free shape (use {ARM_SHAPE} or {E2_MICRO_SHAPE}).")
    if new_disk_gb and new_disk_gb < MIN_BOOT_VOLUME_GB:
        problems.append(f"BOOT_VOLUME_SIZE must be at least {MIN_BOOT_VOLUME_GB} GB.")
    if storage_used_gb + new_disk_gb > FREE_STORAGE_GB:
        problems.append(f"Storage: {storage_used_gb:g} GB in use + {new_disk_gb:g} GB requested "
                        f"> {FREE_STORAGE_GB} GB free.")
    return problems


def storage_used_gb(clients, compartment_id, ad_names):
    """Boot + block volume GB currently in the tenancy (they share the 200 GB free pool)."""
    # shortcut: first page of each list only, enough for a free-tier account; paginate if that changes.
    volumes = list(execute_oci_command(clients.blockstorage, "list_volumes", compartment_id=compartment_id))
    for ad in ad_names:
        volumes += execute_oci_command(clients.blockstorage, "list_boot_volumes",
                                       availability_domain=ad, compartment_id=compartment_id)
    return sum(v.size_in_gbs for v in volumes if v.lifecycle_state not in GONE)


# --------------------------------------------------------------------------- OCI calls with retry

RETRYABLE_CODES = {"TooManyRequests", "Out of host capacity.", "InternalError", "RequestException"}
RETRYABLE_STATUSES = {502, 503, 504}
RETRYABLE_MESSAGES = ("Out of host capacity.", "Bad Gateway", "Max retries exceeded", "ProxyError",
                      "Tunnel connection failed", "Connection aborted", "ConnectTimeout",
                      "Read timed out")


def is_retryable(data):
    message = data.get("message") or ""
    return (data.get("code") in RETRYABLE_CODES
            or data.get("status") in RETRYABLE_STATUSES
            or any(m in message for m in RETRYABLE_MESSAGES))


def handle_errors(command, data):
    """Sleep and return True for temporary errors (capacity, throttling, network); raise otherwise."""
    if is_retryable(data):
        log.info("Command: %s--\nOutput: %s", command, data)
        time.sleep(WAIT_TIME)
        return True
    raise Exception("Error: %s" % data)


def _error_data(err):
    if isinstance(err, oci.exceptions.ServiceError):
        return {"status": err.status, "code": err.code, "message": err.message}
    return {"status": None, "code": "RequestException", "message": str(err)}


def execute_oci_command(client, method, *args, **kwargs):
    """Call client.method(*args, **kwargs), retrying forever on temporary errors."""
    while True:
        try:
            response = getattr(client, method)(*args, **kwargs)
            return response.data if hasattr(response, "data") else response
        except (oci.exceptions.ServiceError, oci.exceptions.RequestException) as err:
            handle_errors(method, _error_data(err))


def make_clients(config):
    return SimpleNamespace(
        identity=oci.identity.IdentityClient(config),
        network=oci.core.VirtualNetworkClient(config),
        compute=oci.core.ComputeClient(config),
        blockstorage=oci.core.BlockstorageClient(config),
    )


# --------------------------------------------------------------------------- network

def find_subnet(settings, clients, compartment_id, ad_names):
    """Configured subnet, or the first usable existing one, or None."""
    if settings.subnet_id:
        return settings.subnet_id
    subnets = execute_oci_command(clients.network, "list_subnets", compartment_id=compartment_id)
    for subnet in subnets:
        if subnet.lifecycle_state != "AVAILABLE":
            continue
        if subnet.availability_domain and subnet.availability_domain not in ad_names:
            continue  # AD-specific subnet in an AD we are not launching in
        if settings.assign_public_ip and subnet.prohibit_public_ip_on_vnic:
            continue  # private subnet: a public IP would be rejected
        return subnet.id
    return None


def create_public_network(clients, compartment_id):
    """Create VCN + internet gateway + 0.0.0.0/0 route + public subnet. Returns the subnet id.

    All of these are free. The default security list already allows SSH (port 22).
    """
    net = clients.network
    models = oci.core.models
    log.info("No usable subnet found: creating a public network")
    vcn = execute_oci_command(net, "create_vcn", models.CreateVcnDetails(
        compartment_id=compartment_id, display_name="free-vcn",
        cidr_blocks=["10.0.0.0/16"], dns_label="freevcn"))
    vcn = oci.wait_until(net, net.get_vcn(vcn.id), "lifecycle_state", "AVAILABLE").data
    igw = execute_oci_command(net, "create_internet_gateway", models.CreateInternetGatewayDetails(
        compartment_id=compartment_id, vcn_id=vcn.id, is_enabled=True, display_name="free-igw"))
    oci.wait_until(net, net.get_internet_gateway(igw.id), "lifecycle_state", "AVAILABLE")
    execute_oci_command(net, "update_route_table", vcn.default_route_table_id,
                        models.UpdateRouteTableDetails(route_rules=[models.RouteRule(
                            destination="0.0.0.0/0", destination_type="CIDR_BLOCK",
                            network_entity_id=igw.id)]))
    subnet = execute_oci_command(net, "create_subnet", models.CreateSubnetDetails(
        compartment_id=compartment_id, vcn_id=vcn.id, cidr_block="10.0.0.0/24",
        display_name="free-public-subnet", dns_label="public", prohibit_public_ip_on_vnic=False,
        route_table_id=vcn.default_route_table_id, security_list_ids=[vcn.default_security_list_id]))
    subnet = oci.wait_until(net, net.get_subnet(subnet.id), "lifecycle_state", "AVAILABLE").data
    log.info("Created subnet %s", subnet.id)
    return subnet.id


# --------------------------------------------------------------------------- notifications

def write_into_file(file_path, data):
    with open(file_path, mode="a", encoding="utf-8") as file_writer:
        file_writer.write(data)


def send_email(subject, body, email, password):
    message = MIMEMultipart()
    message["Subject"] = subject
    message["From"] = email
    message["To"] = email
    message.attach(MIMEText(body, "html"))
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(email, password)
        server.sendmail(email, email, message.as_string())


def desktop_notify(title, text):
    """Pop-up on Windows (always on top), notify-send on Linux, Notification Center on macOS."""
    try:
        if sys.platform == "win32":
            import ctypes
            # MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST; own thread so the script keeps going
            threading.Thread(target=ctypes.windll.user32.MessageBoxW,
                             args=(0, text, title, 0x40 | 0x10000 | 0x40000)).start()
        elif sys.platform == "darwin":
            subprocess.run(["osascript", "-e",
                            f"display notification {json.dumps(text)} with title {json.dumps(title)}"],
                           check=False)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", "-u", "critical", title, text], check=False)
    except Exception as err:  # a missing desktop must never stop the launcher
        log.error("Desktop notification failed: %s", err)


def notify(settings, title, text, desktop=True):
    """Send text to every configured channel. Failures are logged, never raised."""
    if settings.discord_webhook:
        try:
            requests.post(settings.discord_webhook, json={"content": f"{title}\n{text}"},
                          timeout=15).raise_for_status()
        except requests.RequestException as err:
            log.error("Discord notification failed: %s", err)
    if settings.telegram_token and settings.telegram_user_id:
        try:
            requests.post(f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage",
                          data={"chat_id": settings.telegram_user_id, "text": f"{title}\n{text}"},
                          timeout=15).raise_for_status()
        except requests.RequestException as err:
            log.error("Telegram notification failed: %s", err)
    if desktop and settings.desktop_notify:
        desktop_notify(title, text)


def generate_html_body(instance):
    with open("email_content.html", "r", encoding="utf-8") as email_temp:
        html = email_temp.read()
    for placeholder, value in (("INSTANCE_ID", instance.id), ("DISPLAY_NAME", instance.display_name),
                               ("AD", instance.availability_domain), ("SHAPE", instance.shape),
                               ("STATE", instance.lifecycle_state)):
        html = html.replace(f"&lt;{placeholder}&gt;", value)
    return html


def elapsed_summary(start, attempts, now=None):
    """'Got it after 13h 27m and 791 attempts.' start is a time.monotonic() value."""
    minutes = int(((time.monotonic() if now is None else now) - start) // 60)
    hours, minutes = divmod(minutes, 60)
    return f"Got it after {hours}h {minutes:02d}m and {attempts} attempt{'' if attempts == 1 else 's'}."


def announce_instance(settings, instance, title="OCI instance created", summary=""):
    body = "\n".join(([summary, ""] if summary else []) + [
                      f"Instance ID: {instance.id}",
                      f"Display Name: {instance.display_name}",
                      f"Availability Domain: {instance.availability_domain}",
                      f"Shape: {instance.shape}",
                      f"State: {instance.lifecycle_state}", "\n"])
    write_into_file("INSTANCE_CREATED", body)
    if settings.notify_email:
        try:
            send_email(title.upper(), generate_html_body(instance), settings.email, settings.email_password)
        except (smtplib.SMTPException, OSError) as err:
            log.error("Email notification failed: %s", err)
    notify(settings, title, body + "The public IP is in the OCI console (Compute > Instances).")


def notify_on_failure(settings, failure_msg):
    body = ("The script hit an unexpected error and stopped.\n\n"
            "Fix the cause (see the message below) and start it again.\n\n"
            f"{failure_msg}")
    write_into_file("UNHANDLED_ERROR.log", body)
    if settings.notify_email:
        try:
            send_email("OCI INSTANCE CREATION SCRIPT: FAILED", body, settings.email, settings.email_password)
        except (smtplib.SMTPException, OSError) as err:
            log.error("Email notification failed: %s", err)
    notify(settings, "OCI launcher stopped with an error", body)


# --------------------------------------------------------------------------- launch

def find_existing(settings, instances, states=("RUNNING", "PROVISIONING")):
    """The instance that means "we are done" for this shape, or None.

    PROVISIONING counts too, so a restart mid-provisioning does not launch a second one.
    """
    matching = [i for i in instances if i.shape == settings.shape and i.lifecycle_state in states]
    if settings.shape == ARM_SHAPE:
        # only ours: another A1 (e.g. a first 2/12 when asking for a second one) is not success
        mine = [i for i in matching if i.display_name == settings.display_name]
        return mine[0] if mine else None
    wanted = 2 if settings.second_micro else 1
    return matching[-1] if len(matching) >= wanted else None


def check_instance_state_and_write(settings, clients, compartment_id, tries=3, summary=""):
    """Look for the instance up to `tries` times (60 s apart); announce it when found."""
    for attempt in range(tries):
        instances = execute_oci_command(clients.compute, "list_instances", compartment_id=compartment_id)
        found = find_existing(settings, instances)
        if found:
            announce_instance(settings, found, summary=summary)
            return True
        if attempt < tries - 1:
            time.sleep(60)
    return False


def availability_domains(settings, clients, tenancy):
    """(names to launch in, all names). Raises ConfigError if OCT_FREE_AD matches none."""
    ads = [ad.name for ad in execute_oci_command(clients.identity, "list_availability_domains",
                                                 compartment_id=tenancy)]
    chosen = [n for n in ads if any(n.endswith(s) for s in settings.ad_suffixes)]
    if not chosen:
        raise ConfigError(f"OCT_FREE_AD={','.join(settings.ad_suffixes)} matches none of {ads}")
    return chosen, ads


def find_image(settings, clients, tenancy):
    if settings.image_id:
        return settings.image_id
    images = execute_oci_command(clients.compute, "list_images", compartment_id=tenancy,
                                 shape=settings.shape)
    shortened = [{key: json.loads(str(image))[key] for key in IMAGE_LIST_KEYS} for image in images]
    with open("images_list.json", "w", encoding="utf-8") as f:
        json.dump(shortened, f, indent=2)
    for image in sorted(images, key=lambda i: i.time_created, reverse=True):  # newest first
        if (image.operating_system == settings.operating_system
                and image.operating_system_version == settings.os_version):
            return image.id
    raise ConfigError(f"No image '{settings.operating_system} {settings.os_version}' for "
                      f"{settings.shape}. See images_list.json for the available ones.")


def read_or_generate_ssh_public_key(public_key_file):
    path = Path(public_key_file).expanduser()
    if not path.is_file():
        from setup_wizard import generate_ssh_key  # same key code as the wizard
        logging.info("SSH key %s doesn't exist, generating a new pair", path)
        generate_ssh_key(path.with_name(f"{path.stem}_private"), path)
    return path.read_text(encoding="utf-8").strip()


def launch_details(settings, ad_name, tenancy, subnet_id, image_id, ssh_public_key):
    models = oci.core.models
    return models.LaunchInstanceDetails(
        availability_domain=ad_name,
        compartment_id=tenancy,
        create_vnic_details=models.CreateVnicDetails(
            assign_public_ip=settings.assign_public_ip, assign_private_dns_record=True,
            display_name=settings.display_name, subnet_id=subnet_id),
        display_name=settings.display_name,
        shape=settings.shape,
        availability_config=models.LaunchInstanceAvailabilityConfigDetails(
            recovery_action="RESTORE_INSTANCE"),
        instance_options=models.InstanceOptions(are_legacy_imds_endpoints_disabled=True),
        shape_config=models.LaunchInstanceShapeConfigDetails(
            ocpus=settings.ocpus, memory_in_gbs=settings.memory_gb),
        source_details=models.InstanceSourceViaImageDetails(
            source_type="image", image_id=image_id, boot_volume_size_in_gbs=settings.boot_volume_gb),
        metadata={"ssh_authorized_keys": ssh_public_key},
    )


def safety_check(settings, clients, tenancy, all_ads):
    instances = execute_oci_command(clients.compute, "list_instances", compartment_id=tenancy)
    used = storage_used_gb(clients, tenancy, all_ads)
    return free_tier_problems(settings.shape, settings.ocpus, settings.memory_gb,
                              settings.boot_volume_gb, instances, used), used


def check(settings, clients, tenancy):
    """Read-only report. Returns True when a launch would stay inside Always Free."""
    ads, all_ads = availability_domains(settings, clients, tenancy)
    problems, used = safety_check(settings, clients, tenancy, all_ads)
    subnet = find_subnet(settings, clients, tenancy, ads)
    print(f"Availability domains: {', '.join(ads)}")
    print(f"Shape: {settings.shape}  OCPUs: {settings.ocpus:g}  Memory: {settings.memory_gb:g} GB  "
          f"Boot volume: {settings.boot_volume_gb} GB (storage in use: {used:g} GB)")
    print(f"Subnet: {subnet or ('none, a public network will be created' if settings.create_network else 'NONE')}")
    print(f"Image: {find_image(settings, clients, tenancy)}")
    for p in problems:
        print(f"OUTSIDE ALWAYS FREE: {p}")
    if not problems:
        print("OK: this request fits in Always Free.")
    return not problems


def launch_instance(settings, clients, tenancy):
    ads, all_ads = availability_domains(settings, clients, tenancy)
    ad_cycle = itertools.cycle(ads)
    logging.info("OCI_AD_NAME: %s", ads)

    if check_instance_state_and_write(settings, clients, tenancy, tries=1):
        return

    problems, _ = safety_check(settings, clients, tenancy, all_ads)
    if problems:
        raise ConfigError("Refusing to launch, this would not be Always Free:\n- " + "\n- ".join(problems))

    subnet_id = find_subnet(settings, clients, tenancy, ads)
    if not subnet_id:
        if not settings.create_network:
            raise ConfigError("No usable subnet. Set OCI_SUBNET_ID or CREATE_NETWORK=true.")
        subnet_id = create_public_network(clients, tenancy)
    logging.info("OCI_SUBNET_ID: %s", subnet_id)

    image_id = find_image(settings, clients, tenancy)
    logging.info("OCI_IMAGE_ID: %s", image_id)
    ssh_public_key = read_or_generate_ssh_public_key(settings.ssh_key_file)

    start, attempts = time.monotonic(), 0
    while True:
        attempts += 1
        try:
            response = clients.compute.launch_instance(launch_instance_details=launch_details(
                settings, next(ad_cycle), tenancy, subnet_id, image_id, ssh_public_key))
            if response.status == 200:
                log.info("Command: launch_instance\nOutput: %s", response)
                if check_instance_state_and_write(settings, clients, tenancy,
                                                  summary=elapsed_summary(start, attempts)):
                    return
        except oci.exceptions.ServiceError as err:
            if err.code == "LimitExceeded":
                log.info("LimitExceeded, checking whether the instance was created: %s", err.message)
                if check_instance_state_and_write(settings, clients, tenancy,
                                                  summary=elapsed_summary(start, attempts)):
                    return
                # safety_check already ruled out a real overuse, so this is Oracle's false positive
                log.info("LimitExceeded but no instance yet, retrying")
                time.sleep(WAIT_TIME)
                continue
            handle_errors("launch_instance", _error_data(err))
        except oci.exceptions.RequestException as err:
            handle_errors("launch_instance", _error_data(err))


# --------------------------------------------------------------------------- resize

def resize_instance(settings, clients, tenancy):
    """Grow an existing A1.Flex (e.g. 2/12 -> 4/24), retrying while there is no capacity.

    The instance reboots. Picks the A1 named DISPLAY_NAME, or the only A1 if there is just one.
    """
    instances = execute_oci_command(clients.compute, "list_instances", compartment_id=tenancy)
    arm = [i for i in _alive(instances) if i.shape == ARM_SHAPE]
    named = [i for i in arm if i.display_name == settings.display_name]
    candidates = named or (arm if len(arm) == 1 else [])
    if not candidates:
        raise ConfigError(f"Found {len(arm)} A1.Flex instances and none named "
                          f"'{settings.display_name}'. Set DISPLAY_NAME to the one to resize.")
    target = candidates[0]
    current = target.shape_config
    if current.ocpus >= settings.resize_ocpus and current.memory_in_gbs >= settings.resize_memory_gb:
        print(f"{target.display_name} already has {current.ocpus:g} OCPUs / {current.memory_in_gbs:g} GB.")
        return target
    problems = free_tier_problems(ARM_SHAPE, settings.resize_ocpus, settings.resize_memory_gb, 0,
                                  instances, 0, replacing_id=target.id)
    if problems:
        raise ConfigError("Refusing to resize, this would not be Always Free:\n- " + "\n- ".join(problems))
    log.info("Resizing %s from %g/%g to %g/%g", target.display_name, current.ocpus,
             current.memory_in_gbs, settings.resize_ocpus, settings.resize_memory_gb)
    models = oci.core.models
    updated = execute_oci_command(clients.compute, "update_instance", target.id, models.UpdateInstanceDetails(
        shape_config=models.UpdateInstanceShapeConfigDetails(
            ocpus=settings.resize_ocpus, memory_in_gbs=settings.resize_memory_gb)))
    notify(settings, "OCI instance resized",
           f"{target.display_name}: {settings.resize_ocpus:g} OCPUs / {settings.resize_memory_gb:g} GB")
    return updated


# --------------------------------------------------------------------------- entry point

def setup_logging():
    logging.basicConfig(filename="setup_and_info.log", level=logging.INFO,
                        format="%(asctime)s - %(levelname)s - %(message)s")
    handler = logging.FileHandler("launch_instance.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def main(argv=None):
    global WAIT_TIME
    parser = argparse.ArgumentParser(description="Get an Oracle Cloud Always Free instance.")
    parser.add_argument("command", nargs="?", default="launch", choices=["launch", "check", "resize"])
    parser.add_argument("--env", default="oci.env", help="settings file (default: oci.env)")
    args = parser.parse_args(argv)

    load_dotenv(args.env)
    setup_logging()
    try:
        settings = load_settings(os.environ)
        WAIT_TIME = settings.wait_secs
        config = oci.config.from_file(os.path.expanduser(settings.oci_config))
    except (ConfigError, oci.exceptions.ClientError, configparser.Error) as err:
        write_into_file("ERROR_IN_CONFIG.log", f"{err}\n")  # setup_init.sh (Linux) watches this file
        print(f"Configuration error: {err}", file=sys.stderr)
        return 2
    clients = make_clients(config)
    tenancy = config["tenancy"]

    if args.command == "check":
        try:
            return 0 if check(settings, clients, tenancy) else 1
        except ConfigError as err:
            print(err, file=sys.stderr)
            return 2

    notify(settings, "OCI launcher started",
           f"{args.command}: {settings.shape} {settings.ocpus:g} OCPU / {settings.memory_gb:g} GB",
           desktop=False)
    try:
        if args.command == "resize":
            resize_instance(settings, clients, tenancy)
        else:
            launch_instance(settings, clients, tenancy)
        return 0
    except Exception as err:
        logging.exception("Launcher stopped")
        if isinstance(err, ConfigError):
            write_into_file("ERROR_IN_CONFIG.log", f"{err}\n")
        notify_on_failure(settings, str(err))
        return 1


if __name__ == "__main__":
    sys.exit(main())
