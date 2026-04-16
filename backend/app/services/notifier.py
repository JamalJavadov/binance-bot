import subprocess

from app.core.logging import get_logger

logger = get_logger(__name__)

SOUND_MAP = {
    "signal": "/System/Library/Sounds/Glass.aiff",
    "placed": "/System/Library/Sounds/Submarine.aiff",
    "tp": "/System/Library/Sounds/Hero.aiff",
    "sl": "/System/Library/Sounds/Basso.aiff",
    "expired": "/System/Library/Sounds/Morse.aiff",
}


class Notifier:
    def __init__(self) -> None:
        try:
            from plyer import notification
        except Exception:
            notification = None
        self.notification = notification

    async def send(self, *, title: str, message: str, sound: str | None = None) -> None:
        if self.notification is not None:
            try:
                self.notification.notify(
                    title=title,
                    message=message,
                    app_name="Futures Bot",
                    timeout=10,
                )
            except Exception:
                logger.warning("notifier.failed", title=title)
        if sound and sound in SOUND_MAP:
            try:
                subprocess.Popen(["afplay", SOUND_MAP[sound]])
            except Exception:
                logger.warning("sound.failed", sound=sound)

