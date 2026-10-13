"""Lifecycle primitives for replaceable policy/planner children, never SDK owners.

Only their background owner calls these blocking functions. Protocol validation
stays with each client; a timed-out pipe must be discarded before another RPC.
"""

from multiprocessing.connection import wait


def start_pipe_process(context, target, *, args=(), name):
    parent, child = context.Pipe()
    process = context.Process(target=target, args=(child, *args), daemon=True, name=name)
    try:
        process.start()
    except Exception:
        parent.close()
        child.close()
        raise
    child.close()
    return process, parent


def receive_process_reply(process, conn, *, timeout, label):
    # Wait for data OR child exit in one OS wait, instead of periodic polling.
    ready = wait([conn, process.sentinel], timeout=timeout)
    if conn in ready:
        try:
            return conn.recv()
        except EOFError:
            raise RuntimeError(f"{label}: process exited")
    if ready:
        raise RuntimeError(f"{label}: process exited")
    raise TimeoutError(f"{label} timeout")


def close_pipe_process(process, conn, *, timeout=0.5):
    """Bounded reap; do not leave an old child beside its replacement."""
    if conn is not None:
        conn.close()
    if process is None:
        return
    if process.is_alive():
        process.terminate()
    process.join(timeout=timeout)
    if process.is_alive():
        process.kill()
        process.join(timeout=timeout)
    if process.is_alive():
        raise RuntimeError(f"{process.name} did not stop; replacement refused")
