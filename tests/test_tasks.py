import threading

from anker_client.core.tasks import BackgroundTask


def test_background_task_emits_result_and_finished():
    results = []
    finished = []

    task = BackgroundTask(lambda _cancel, value: value * 2, 21)
    task.signals.result.connect(results.append)
    task.signals.finished.connect(lambda: finished.append(True))
    task.run()

    assert results == [42]
    assert finished == [True]


def test_cancelled_background_task_does_not_run_or_emit_result():
    called = []
    results = []
    finished = []

    def work(_cancel: threading.Event):
        called.append(True)
        return "unexpected"

    task = BackgroundTask(work)
    task.signals.result.connect(results.append)
    task.signals.finished.connect(lambda: finished.append(True))
    task.cancel()
    task.run()

    assert called == []
    assert results == []
    assert finished == [True]
