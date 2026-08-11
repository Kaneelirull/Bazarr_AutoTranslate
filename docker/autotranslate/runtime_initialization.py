from . import runtime_context as _runtime

def initialize_runtime_state():
    _runtime._episode_cache: dict[int, int] = {}
    _runtime._movie_cache: dict[int, int] = {}
    _runtime._media_cache_lock = _runtime.threading.Lock()
    _runtime._cycle_suppressions = _runtime.CycleSuppressionRegistry()
    _runtime._translation_capacity = _runtime.TranslationCapacityGate(_runtime.PARALLEL_TRANSLATES)
    _runtime._shared_capacity = _runtime.SharedCapacityCoordinator(_runtime.PARALLEL_TRANSLATES)
    _runtime._file_lane_gate = _runtime.FileLaneGate(_runtime.PARALLEL_TRANSLATES)
    _runtime.shutdown_requested = False
    _runtime._shutdown_controller = _runtime.ShutdownController(_runtime.REPAIR_SHUTDOWN_GRACE_SECONDS)
    _runtime.signal.signal(_runtime.signal.SIGTERM, _runtime._handle_signal)
    _runtime.signal.signal(_runtime.signal.SIGINT, _runtime._handle_signal)
    _runtime._TIMESTAMP_RE = _runtime._re.compile('^\\d{2}:\\d{2}:\\d{2},\\d{3} --> \\d{2}:\\d{2}:\\d{2},\\d{3}$')
    _runtime._LANGUAGE_ALIASES = {'en': {'en', 'eng'}, 'et': {'et', 'est'}, 'sv': {'sv', 'swe'}, 'de': {'de', 'deu', 'ger'}, 'fr': {'fr', 'fra', 'fre'}, 'es': {'es', 'spa'}, 'nl': {'nl', 'nld', 'dut'}, 'no': {'no', 'nor', 'nob'}, 'fi': {'fi', 'fin'}, 'da': {'da', 'dan'}, 'pl': {'pl', 'pol'}, 'pt': {'pt', 'por'}, 'ru': {'ru', 'rus'}, 'lv': {'lv', 'lav'}, 'lt': {'lt', 'lit'}, 'uk': {'uk', 'ukr'}, 'tr': {'tr', 'tur'}, 'it': {'it', 'ita'}, 'cs': {'cs', 'ces', 'cze'}, 'sk': {'sk', 'slk', 'slo'}, 'hu': {'hu', 'hun'}, 'ro': {'ro', 'ron', 'rum'}, 'el': {'el', 'ell', 'gre'}, 'ar': {'ar', 'ara'}, 'he': {'he', 'heb'}, 'ja': {'ja', 'jpn'}, 'ko': {'ko', 'kor'}, 'zh': {'zh', 'zho', 'chi'}}
    _runtime._ALIAS_TO_LANGUAGE = {_runtime.alias: _runtime.code for _runtime.code, _runtime.aliases in _runtime._LANGUAGE_ALIASES.items() for _runtime.alias in _runtime.aliases}
    _runtime._VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.mov', '.m4v', '.ts', '.webm'}
    _runtime._NON_FULL_SUBTITLE_TOKENS = {'forced', 'foreign', 'signs', 'commentary'}
