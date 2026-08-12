"""
Subprocess worker — extracts ONE file in its own process so the orchestrator can
KILL it on timeout. A hung Gemini/Vertex call cannot be cancelled inside a
thread; a separate process can be terminated, which frees the concurrency slot
and prevents pool starvation (the failure that stalled the thread-pool run).

Deliberately imports nothing Django at module load — the child imports this
module before django.setup() has run — so all heavy imports live inside run().
"""
import os
import sys


def run(label, path, backend_root, q):
    sys.path.insert(0, backend_root)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    os.environ.setdefault('TFAI_ENV', 'local')
    try:
        import django
        django.setup()
        from dataimport.preingest2.extractor import extract_file
        rec = extract_file(label, path, heartbeat=lambda stage: q.put(('hb', stage)))
        q.put(('ok', rec))
    except Exception as e:  # noqa: BLE001
        import traceback
        q.put(('err', f'{e.__class__.__name__}: {e}\n{traceback.format_exc()[-500:]}'))
