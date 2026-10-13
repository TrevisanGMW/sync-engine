"""Shows dependency installation progress using the standard-library Tk UI."""

import queue
import threading


def run_with_progress(setup_function):
    """Runs setup while displaying a small progress window when Tk is available.

    Args:
        setup_function (callable): Setup function accepting a status callback.

    Returns:
        object: Result returned by the setup function.

    Raises:
        Exception: Any error raised by the setup function.
    """
    try:
        import tkinter as tk
        from tkinter import ttk
        root = tk.Tk()
    except Exception:
        return setup_function(lambda message: None)

    root.title("Preparing SyncEngine")
    root.resizable(False, False)
    root.geometry("430x128")
    root.protocol("WM_DELETE_WINDOW", lambda: None)
    frame = ttk.Frame(root, padding=18)
    frame.pack(fill="both", expand=True)
    title = ttk.Label(frame, text="Setting up SyncEngine's private runtime...")
    title.pack(anchor="w")
    message = ttk.Label(frame, text="Checking Python requirements...")
    message.pack(anchor="w", pady=(8, 10))
    progress = ttk.Progressbar(frame, mode="indeterminate", length=390)
    progress.pack(fill="x")
    progress.start(12)
    events = queue.Queue()

    def report(text):
        events.put(("message", str(text)))

    def work():
        try:
            events.put(("result", setup_function(report), None))
        except Exception as error:
            events.put(("result", None, error))

    threading.Thread(target=work, name="SyncEngineDependencySetup", daemon=True).start()
    result = {"value": None, "error": None}

    def poll():
        try:
            while True:
                event = events.get_nowait()
                if event[0] == "message":
                    message.configure(text=event[1])
                else:
                    result["value"], result["error"] = event[1], event[2]
                    root.quit()
                    return
        except queue.Empty:
            root.after(100, poll)

    root.after(100, poll)
    root.mainloop()
    progress.stop()
    root.destroy()
    if result["error"] is not None:
        raise result["error"]
    return result["value"]
