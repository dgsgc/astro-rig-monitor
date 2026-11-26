"""The monitor's scheduled task: runs armwin.launcher at logon, for this user, with no time limit
and on battery too. Managed with schtasks.exe, which every Windows has.

    pythonw -m armwin.task install | remove | start | stop | status
"""

import getpass
import json
import os
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape

from . import paths

CREATE_NO_WINDOW = 0x08000000


def schtasks(*args: str):
    p = subprocess.run(["schtasks", *args], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    return p.returncode, (p.stdout + p.stderr).strip()


def user() -> str:
    domain = os.environ.get("USERDOMAIN")
    name = os.environ.get("USERNAME") or getpass.getuser()
    return f"{domain}\\{name}" if domain else name


def xml() -> str:
    u = escape(user())
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Astro Rig Monitor (read-only to the rigs). App {escape(paths.version())}, build {escape(paths.build())}.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{u}</UserId></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>{u}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(paths.PYTHONW))}</Command>
      <Arguments>{" ".join(paths.PY_FLAGS)} -m armwin.launcher</Arguments>
      <WorkingDirectory>{escape(str(paths.APP))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def state() -> dict:
    try:
        return json.loads(paths.STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(**changes):
    s = state()
    s.update(changes)
    paths.DATA.mkdir(parents=True, exist_ok=True)
    paths.STATE.write_text(json.dumps(s, indent=1), encoding="utf-8")


def installed() -> bool:
    return schtasks("/Query", "/TN", paths.TASK)[0] == 0


def out_of_date() -> bool:
    """Installed for another copy or build of the app (moved, or updated)?"""
    s = state()
    return s.get("task_command") != str(paths.PYTHONW) or s.get("task_build") != paths.build()


def install():
    """Write the task (replacing any old one) and start it."""
    stop()
    paths.DATA.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-16") as f:
            f.write(xml())
        code, out = schtasks("/Create", "/TN", paths.TASK, "/XML", tmp, "/F")
    finally:
        os.remove(tmp)
    if code != 0:
        raise RuntimeError(f"couldn't add the scheduled task: {out}")
    save_state(task_command=str(paths.PYTHONW), task_build=paths.build(), off=False)
    start()


def remove():
    stop()
    schtasks("/Delete", "/TN", paths.TASK, "/F")


def start():
    schtasks("/Change", "/TN", paths.TASK, "/ENABLE")
    return schtasks("/Run", "/TN", paths.TASK)


def stop():
    return schtasks("/End", "/TN", paths.TASK)


def disable():
    stop()
    schtasks("/Change", "/TN", paths.TASK, "/DISABLE")
    save_state(off=True)


def status() -> str:
    """'Running', 'Ready' (stopped), 'Disabled', or '' if there's no task. Through PowerShell,
    because schtasks prints the status in the PC's own language."""
    p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        f"(Get-ScheduledTask -TaskName '{paths.TASK}' -ErrorAction SilentlyContinue).State"],
                       capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    return p.stdout.strip()


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"install": install, "remove": remove, "start": start, "stop": stop}.get(what, lambda: None)()
    print(status() or "no task")
