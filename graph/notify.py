"""Best-effort desktop notifications for a run (completion, failures, rejections). Never raises, never blocks,
opens no window, and writes no files: title and message reach the OS notifier through the environment."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

_WIN_TOAST = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] "
    "| Out-Null;"
    "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('ToastText02');"
    "$t=$x.GetElementsByTagName('text');"
    "$t.Item(0).AppendChild($x.CreateTextNode($env:BUFFETT_NOTIFY_TITLE)) | Out-Null;"
    "$t.Item(1).AppendChild($x.CreateTextNode($env:BUFFETT_NOTIFY_MESSAGE)) | Out-Null;"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
    "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
    ".Show([Windows.UI.Notifications.ToastNotification]::new($x))"
)


def notify(title: str, message: str) -> None:
    """Show a desktop notification. Failure to notify must never affect the workflow."""
    message = " ".join(message.split())[:250]
    env = {**os.environ, "BUFFETT_NOTIFY_TITLE": title, "BUFFETT_NOTIFY_MESSAGE": message}
    try:
        if os.name == "nt":
            cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _WIN_TOAST]
            flags = subprocess.CREATE_NO_WINDOW
        elif sys.platform == "darwin":
            cmd = ["osascript", "-e", 'display notification (system attribute "BUFFETT_NOTIFY_MESSAGE") '
                   'with title (system attribute "BUFFETT_NOTIFY_TITLE")']
            flags = 0
        elif shutil.which("notify-send"):
            cmd, flags = ["notify-send", title, message], 0
        else:
            return
        subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags)
    except Exception:
        pass
