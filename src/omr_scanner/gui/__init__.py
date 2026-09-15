"""PySide6 presentation layer.

Purpose:
    Windows, pages, dialogs and the Qt application object.

Responsibilities:
    * Show state and collect user intent.
    * Call services and turn the exceptions they raise into readable messages.

What does NOT belong here (enforced by ``tests/unit/test_architecture.py``):
    * OpenCV calls or any pixel algorithm.
    * SQL, SQLAlchemy sessions or direct database access.
    * File system layout decisions. The GUI asks the user *where*; the service
      decides *what* to write there.

Rule of thumb:
    If a piece of logic would still be meaningful in a command line version of
    OMRFlow, it belongs in a service, not in a widget.
"""
