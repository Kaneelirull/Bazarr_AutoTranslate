from __future__ import annotations


class CueRepairProvider:
    """Shared request/attempt contract for ordinary and ensemble cue recovery."""

    def __init__(self, translate, store, identity, config_fingerprint, *, cancelled=lambda: False):
        self.translate = translate
        self.store = store
        self.identity = identity
        self.config_fingerprint = config_fingerprint
        self.cancelled = cancelled
        self.event = {}

    def on_attempt(self, event):
        self.event = dict(event)
        if event.get('event') != 'rejected' or not event.get('outputFingerprint') or not self.identity.get('sourceHash') or self.identity.get('itemType') not in ('episodes', 'movies') or self.identity.get('itemId') is None:
            return
        return self.store.record_failure_fingerprint(
            item_type=self.identity['itemType'], item_id=self.identity['itemId'],
            target_language=self.identity['targetLanguage'], source_file_hash=self.identity['sourceHash'],
            source_cue_hash=event['sourceCueHash'], strategy_key=event.get('strategy') or 'unknown',
            provider='lingarr', config_fingerprint=self.config_fingerprint,
            output_fingerprint=event['outputFingerprint'], failure_class=','.join(event.get('validationRules') or ['validation']))

    def __call__(self, line, before, after):
        if self.cancelled():
            return None, {'cancelled': True}
        metadata = {}
        translated = self.translate(line, self.identity.get('sourceLanguage') or 'en',
            self.identity['targetLanguage'], before, after,
            repair_label=self.identity.get('mediaTitle') or 'subtitle recovery',
            cue_number=self.event.get('cueNumber'), attempt=self.event.get('attempt'),
            outcome_meta=metadata, strict=self.event.get('strategy') == 'strict_isolated',
            cancellation_requested=self.cancelled)
        return translated, metadata
