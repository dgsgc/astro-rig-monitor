"""Astro Rig Monitor for Windows: the tray icon, the background launcher and its scheduled task.

The installed layout (per user, no admin rights needed):

    %LOCALAPPDATA%\\Programs\\Astro Rig Monitor\\
        python\\              Python 3.12 with paho-mqtt, Pillow, certifi, pystray and qrcode
        app\\arm\\            the monitor, the same code as everywhere else
        app\\armwin\\         this package
        icon.ico, VERSION, BUILD

    %LOCALAPPDATA%\\Astro Rig Monitor\\   config.toml, arm.log, console.log, nights\\, reports\\

A scheduled task ("Astro Rig Monitor", at logon) runs `pythonw -m armwin.launcher`, which keeps the
PC awake and runs the monitor, restarting it if it stops. The tray icon (`pythonw -m armwin.tray`)
only shows status and opens things; quitting it doesn't stop the monitor.
"""
