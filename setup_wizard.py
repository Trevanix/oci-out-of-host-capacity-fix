"""Interactive setup: creates the OCI API key and the SSH key, then writes oci_config and oci.env.

Run:  python setup_wizard.py
Nothing is sent anywhere: keys are created locally and you paste the public one in the OCI console.
"""
import hashlib
import os
import re
import shutil
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

HERE = Path(__file__).resolve().parent
API_KEY = HERE / "oci_api_key.pem"
API_PUBLIC_KEY = HERE / "oci_api_key_public.pem"
SSH_KEY = HERE / "ssh_key"
SSH_PUBLIC_KEY = HERE / "ssh_key.pub"
OCI_CONFIG = HERE / "oci_config"
OCI_ENV = HERE / "oci.env"

REGION_RE = re.compile(r"^[a-z]{2}-[a-z]+-\d+$")


# --------------------------------------------------------------------------- keys

def _write_private(path, data):
    path.write_bytes(data)
    if os.name != "nt":
        os.chmod(path, 0o600)


def fingerprint(public_pem):
    """OCI API key fingerprint: MD5 of the DER public key, as aa:bb:..."""
    der = serialization.load_pem_public_key(public_pem).public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    digest = hashlib.md5(der).hexdigest()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def generate_api_key(private_path, public_path):
    """RSA 2048 PEM pair for the OCI API (the format the console asks for). Returns the fingerprint."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _write_private(private_path, key.private_bytes(serialization.Encoding.PEM,
                                                   serialization.PrivateFormat.PKCS8,
                                                   serialization.NoEncryption()))
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo)
    Path(public_path).write_bytes(public)
    return fingerprint(public)


def generate_ssh_key(private_path, public_path):
    """RSA 4096 OpenSSH key pair to log in to the instance (ssh -i <private> ubuntu@<ip>)."""
    private_path, public_path = Path(private_path), Path(public_path)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    _write_private(private_path, key.private_bytes(serialization.Encoding.PEM,
                                                   serialization.PrivateFormat.OpenSSH,
                                                   serialization.NoEncryption()))
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                           serialization.PublicFormat.OpenSSH)
    public_path.write_bytes(public + b" oci-free-instance\n")


# --------------------------------------------------------------------------- files

def parse_config_preview(text):
    """Read the 'Configuration file preview' the OCI console shows after adding an API key."""
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in ("user", "fingerprint", "tenancy", "region"):
            values[key.strip()] = value.strip()
    return values


def validate_identity(values):
    """Return a list of problems with user / tenancy / region / fingerprint."""
    problems = []
    if not values.get("user", "").startswith("ocid1.user."):
        problems.append("user must start with ocid1.user.")
    if not values.get("tenancy", "").startswith("ocid1.tenancy."):
        problems.append("tenancy must start with ocid1.tenancy.")
    if not REGION_RE.match(values.get("region", "")):
        problems.append("region looks wrong (example: eu-madrid-1)")
    if not re.fullmatch(r"([0-9a-f]{2}:){15}[0-9a-f]{2}", values.get("fingerprint", "")):
        problems.append("fingerprint looks wrong (16 hex pairs separated by ':')")
    return problems


def render_oci_config(values, key_file):
    return ("[DEFAULT]\n"
            f"user={values['user']}\n"
            f"fingerprint={values['fingerprint']}\n"
            f"tenancy={values['tenancy']}\n"
            f"region={values['region']}\n"
            f"key_file={Path(key_file).as_posix()}\n")


def _quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_env(settings):
    """oci.env text. Values are double-quoted so spaces are fine (python-dotenv unquotes them)."""
    return "".join(f"{k}={_quote(v)}\n" for k, v in settings.items())


def backup_and_write(path, text):
    if path.exists():
        shutil.copyfile(path, path.with_name(path.name + ".bak"))
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- prompts

def ask(prompt, default="", check=None):
    while True:
        answer = input(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip() or default
        error = check(answer) if check else None
        if not error:
            return answer
        print(f"  -> {error}")


def _number(answer):
    try:
        float(answer)
    except ValueError:
        return "type a number"
    return None


def main():
    from main import ARM_SHAPE, E2_MICRO_SHAPE, free_tier_problems

    print("Oracle Cloud Always Free launcher - setup\n")
    print(f"Files go to: {HERE}\n")

    # 1. API key
    if API_KEY.exists() and API_PUBLIC_KEY.exists():
        fp = fingerprint(API_PUBLIC_KEY.read_bytes())
        print(f"Using the existing API key {API_KEY.name} (fingerprint {fp}).")
    else:
        fp = generate_api_key(API_KEY, API_PUBLIC_KEY)
        print(f"Created the API key {API_KEY.name} (fingerprint {fp}).")
    print("\nUpload its PUBLIC part to Oracle Cloud:\n"
          "  1. Open https://cloud.oracle.com and sign in.\n"
          "  2. Profile icon (top right) > My profile > API keys > Add API key.\n"
          "  3. Choose 'Paste a public key' and paste this:\n")
    print(API_PUBLIC_KEY.read_text(encoding="utf-8"))
    print("  4. Click Add. Oracle shows a 'Configuration file preview'.\n")

    print("Paste that preview here and press Enter on an empty line\n"
          "(or just press Enter to type the values one by one):")
    lines = []
    while True:
        line = input()
        if not line.strip():
            break
        lines.append(line)
    values = parse_config_preview("\n".join(lines))
    values.setdefault("fingerprint", fp)
    for key, hint in (("user", "User OCID (ocid1.user...)"), ("tenancy", "Tenancy OCID (ocid1.tenancy...)"),
                      ("region", "Region (e.g. eu-madrid-1)")):
        if key not in values:
            values[key] = ask(hint)
    while problems := validate_identity(values):
        print("Please fix: " + "; ".join(problems))
        for key in ("user", "tenancy", "region", "fingerprint"):
            values[key] = ask(key, values.get(key, ""))
    if values["fingerprint"] != fp:
        print(f"WARNING: the console fingerprint {values['fingerprint']} is not this key's ({fp}).\n"
              "  You probably pasted another key; upload the one shown above.")

    # 2. What to launch
    print("\nWhat do you want to launch?\n  1) Ampere A1.Flex (ARM, up to 4 OCPU / 24 GB free)\n"
          "  2) AMD E2.1.Micro (1/8 OCPU, 1 GB, up to 2 free)")
    arm = ask("Choose", "1", lambda a: None if a in ("1", "2") else "type 1 or 2") == "1"
    shape = ARM_SHAPE if arm else E2_MICRO_SHAPE
    ocpus, memory = ("2", "12") if arm else ("1", "1")
    if arm:
        print("Tip: 2 OCPU / 12 GB is found faster; you can grow it later with 'python main.py resize'.")
        while True:
            ocpus = ask("OCPUs", "2", _number)
            memory = ask("Memory GB", "12", _number)
            problems = free_tier_problems(shape, float(ocpus), float(memory), 50, [], 0)
            if not problems:
                break
            print("  -> " + " ".join(problems))
    boot = ask("Boot volume GB (50-200)", "50",
               lambda a: None if a.isdigit() and 50 <= int(a) <= 200 else "between 50 and 200")
    os_version = ask("Ubuntu version", "22.04")
    ad = ask("Availability domain suffix(es), comma separated", "AD-1")
    name = ask("Instance name", "free-instance")

    # 3. Notifications (all optional)
    print("\nNotifications. A desktop pop-up is always shown; the rest is optional (Enter to skip).")
    discord = ask("Discord webhook URL")
    telegram_token = ask("Telegram bot token")
    telegram_user = ask("Telegram user ID") if telegram_token else ""
    email = ask("Gmail address")
    email_password = ask("Gmail app password (16 characters)") if email else ""

    # 4. SSH key
    if not SSH_PUBLIC_KEY.exists():
        generate_ssh_key(SSH_KEY, SSH_PUBLIC_KEY)
        print(f"\nCreated the SSH key {SSH_KEY.name} (keep it private; you log in with it).")

    backup_and_write(OCI_CONFIG, render_oci_config(values, API_KEY))
    backup_and_write(OCI_ENV, render_env({
        "OCI_CONFIG": OCI_CONFIG.as_posix(),
        "OCT_FREE_AD": ad,
        "DISPLAY_NAME": name,
        "OCI_COMPUTE_SHAPE": shape,
        "OCPUS": ocpus,
        "MEMORY_GB": memory,
        "RESIZE_OCPUS": "4",
        "RESIZE_MEMORY_GB": "24",
        "SECOND_MICRO_INSTANCE": "False",
        "REQUEST_WAIT_TIME_SECS": "60",
        "SSH_AUTHORIZED_KEYS_FILE": SSH_PUBLIC_KEY.as_posix(),
        "OCI_SUBNET_ID": "",
        "CREATE_NETWORK": "True",
        "OCI_IMAGE_ID": "",
        "OPERATING_SYSTEM": "Canonical Ubuntu",
        "OS_VERSION": os_version,
        "ASSIGN_PUBLIC_IP": "True",
        "BOOT_VOLUME_SIZE": boot,
        "DESKTOP_NOTIFY": "True",
        "NOTIFY_EMAIL": "True" if email else "False",
        "EMAIL": email,
        "EMAIL_PASSWORD": email_password,
        "DISCORD_WEBHOOK": discord,
        "TELEGRAM_TOKEN": telegram_token,
        "TELEGRAM_USER_ID": telegram_user,
    }))
    print(f"\nWrote {OCI_CONFIG.name} and {OCI_ENV.name}.\n"
          "Next:\n  python main.py check   (read-only test of the setup)\n"
          "  python main.py         (start trying; leave it running)")
    print(f"\nWhen it is created, log in with:\n  ssh -i \"{SSH_KEY}\" ubuntu@<public IP>")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        sys.exit("\nCancelled.")
