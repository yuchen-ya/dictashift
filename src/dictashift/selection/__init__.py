from .capture import CaptureResult, CaptureStatus, capture_selection, simulate_copy
from .clipboard_backup import ClipboardBackupError, ClipboardSnapshot, backup_clipboard
from .service import SelectionEvent, SelectionService

__all__ = [
    "CaptureResult",
    "CaptureStatus",
    "ClipboardBackupError",
    "ClipboardSnapshot",
    "SelectionEvent",
    "SelectionService",
    "backup_clipboard",
    "capture_selection",
    "simulate_copy",
]
