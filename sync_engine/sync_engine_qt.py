"""Local Qt adapter for the standalone SyncEngine application."""

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    IS_PYSIDE6 = True
except ImportError:
    from PySide2 import QtCore, QtGui, QtWidgets
    IS_PYSIDE6 = False

Signal = QtCore.Signal
Slot = QtCore.Slot


def qt_enum(owner, group, name):
    """Resolves an enum across supported PySide versions.

    Args:
        owner (object): Qt class containing the enum.
        group (str): Scoped enum name used by newer PySide versions.
        name (str): Enum member name.

    Returns:
        object: Qt enum value.
    """
    scoped = getattr(owner, group, None)
    return getattr(scoped, name) if scoped is not None else getattr(owner, name)


def application_exec(application):
    """Runs the Qt application using the supported binding's entry point.

    Args:
        application (QApplication): Application instance.

    Returns:
        int: Application exit code.
    """
    runner = getattr(application, "exec", None) or application.exec_
    return runner()
