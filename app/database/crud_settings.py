from typing import Any
from .session import SessionLocal
from .models import SystemSettings

def get_system_setting(key: str, default: Any = None) -> Any:
    """Read a global setting from the system_settings table."""
    db = SessionLocal()
    try:
        setting = db.query(SystemSettings).filter(SystemSettings.key == key).first()
        if setting:
            return setting.value
        return default
    finally:
        db.close()

def set_system_setting(key: str, value: Any, description: str = None) -> None:
    """Save a global setting to the system_settings table."""
    db = SessionLocal()
    try:
        setting = db.query(SystemSettings).filter(SystemSettings.key == key).first()
        if setting:
            setting.value = value
            if description is not None:
                setting.description = description
        else:
            setting = SystemSettings(key=key, value=value, description=description)
            db.add(setting)
        db.commit()
    finally:
        db.close()

def get_all_system_settings() -> dict[str, Any]:
    """Read all global settings as a dictionary."""
    db = SessionLocal()
    try:
        settings = db.query(SystemSettings).all()
        return {s.key: s.value for s in settings}
    finally:
        db.close()
