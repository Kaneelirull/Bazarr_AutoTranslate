# Offline recovery fixtures

These five English sources and their Swedish failed attempts reproduce the retry
recovery failures investigated on 2026-09-02. `replay-records.json` contains only
the item/attempt identifiers, titles and timestamps needed to replay them.
Raw server logs, settings, database records and local capture scripts are excluded.

`tests/test_name_recovery.py` replays the fixtures through the production scheduler
with network and AI calls forbidden. Three cases recover from saved donors; the
two remaining cases require exact, scoped name approval before publication.
