# Instructions for AI coding agents

You are helping a person get an Oracle Cloud **Always Free** instance with this repo. Work in this folder.
Explain each step in plain words; the person may not be technical. Reply in the person's language.
If the person is changing the code instead of setting it up, skip the setup guide and keep the tests passing.

## What you can and cannot do

- You **cannot** sign in to Oracle Cloud for the person, and you must not ask for their Oracle password.
  They sign in themselves and paste back what Oracle shows.
- Never print, commit or upload `oci_config`, `oci.env`, `*.pem`, `ssh_key*` or logs. They are in `.gitignore`.
- Never set a shape, OCPU/RAM or disk outside Always Free. `main.py` refuses it anyway; do not work around it.
- Run `python main.py check` (read-only) before anything that creates resources.

## Setup, step by step

1. **Python**: needs 3.12 (3.11 and 3.13 also work; 3.14+ may fail to install `oci`).
   - Windows: `powershell -ExecutionPolicy Bypass -File setup_windows.ps1` (creates `.venv`, installs, starts the wizard).
   - Linux/macOS: `python3.12 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`.
2. **Configuration**: `python setup_wizard.py` is interactive. Prefer asking the person to run it in their own
   terminal. If you cannot type into it, do the same thing yourself:
   - Create the API key with `setup_wizard.generate_api_key(...)` (files `oci_api_key.pem` / `oci_api_key_public.pem`)
     unless they already exist, and show the person the **public** key only.
   - Tell them: Oracle Cloud console → profile icon (top right) → **User settings** → **Tokens and keys** →
     **Add API key** → **Paste a public key** → paste → **Add**. Oracle then shows a **Configuration file preview**.
   - Ask them to paste that preview (it contains OCIDs, region and fingerprint, no password).
   - Write `oci_config` with `setup_wizard.render_oci_config(...)` and `oci.env` with `setup_wizard.render_env(...)`,
     using the same keys and defaults as `setup_wizard.main()`. Default size: `VM.Standard.A1.Flex`, 2 OCPU / 12 GB,
     50 GB disk. Create the SSH key with `setup_wizard.generate_ssh_key(...)` if `ssh_key.pub` is missing.
3. **Check**: `python main.py check`. It must end with `OK: this request fits in Always Free.`
4. **Start**:
   - Windows, keep trying across reboots: `powershell -ExecutionPolicy Bypass -File start_windows.ps1 autostart`
   - Windows, only now: `powershell -ExecutionPolicy Bypass -File start_windows.ps1`
   - Linux/macOS: `nohup python main.py > python_error.log 2>&1 &` (for reboots, see "Start automatically" in README.md).
5. **Wait**: "Out of host capacity" in `launch_instance.log` is normal; it retries every minute and can take hours or days.
   When it succeeds a pop-up says how long it took and how many attempts it needed.
6. **After success**: Windows autostart → `start_windows.ps1 remove-autostart`. Optional: `python main.py resize`
   grows a 2/12 instance to 4/24 (the instance reboots).

## Troubleshooting

See `TROUBLESHOOTING.md`. Run the tests with `python -m unittest discover tests` (no real Oracle calls).
