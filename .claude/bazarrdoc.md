# Bazarr HTTP API Documentation

**Version:** 1.5.6  
**Base URL:** `/api`  
**Description:** API docs for Bazarr  

---

## Table of Contents

- [Badges](#badges)
- [Episodes](#episodes)
- [Episodes Blacklist](#episodes-blacklist)
- [Episodes History](#episodes-history)
- [Episodes Subtitles](#episodes-subtitles)
- [Episodes Wanted](#episodes-wanted)
- [Files Browser for Bazarr](#files-browser-for-bazarr)
- [Files Browser for Radarr](#files-browser-for-radarr)
- [Files Browser for Sonarr](#files-browser-for-sonarr)
- [History Statistics](#history-statistics)
- [Movies](#movies)
- [Movies Blacklist](#movies-blacklist)
- [Movies History](#movies-history)
- [Movies Subtitles](#movies-subtitles)
- [Movies Wanted](#movies-wanted)
- [Providers](#providers)
- [Providers Episodes](#providers-episodes)
- [Providers Movies](#providers-movies)
- [Series](#series)
- [Subtitles](#subtitles)
- [Subtitles Info](#subtitles-info)
- [System Announcements](#system-announcements)
- [System Backups](#system-backups)
- [System Health](#system-health)
- [System Languages](#system-languages)
- [System Languages Profiles](#system-languages-profiles)
- [System Logs](#system-logs)
- [System Ping](#system-ping)
- [System Releases](#system-releases)
- [System Searches](#system-searches)
- [systemSettings](#systemsettings)
- [System Status](#system-status)
- [System Tasks](#system-tasks)
- [System Jobs](#system-jobs)
- [Webhooks Plex](#webhooks-plex)
- [Webhooks Radarr](#webhooks-radarr)
- [Webhooks Sonarr](#webhooks-sonarr)
- [Plex Authentication](#plex-authentication)
- [Other](#other)
- [Models / Definitions](#models--definitions)

---

## Badges

> Get badges count to update the UI (episodes and movies wanted subtitles, providers with issues, health issues and announcements.

### `GET /api/badges`

**Summary:** Get badges count to update the UI

**Operation ID:** `get_badges`

**Parameters:**

_No parameters_

**Responses**

- `401`: Not Authenticated

---

## Episodes

> List episodes metadata for specific series or episodes.

### `GET /api/episodes`

**Summary:** List episodes metadata for specific series or episodes

**Operation ID:** `get_episodes`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `seriesid[]` | query | array<integer> | No | Series IDs to list episodes for (default: `[]`) |
| `episodeid[]` | query | array<integer> | No | Episodes ID to list (default: `[]`) |

**Responses**

- `200`: Success
- `401`: Not Authenticated
- `404`: Series or Episode ID not provided

---

## Episodes Blacklist

> List, add or remove subtitles to or from episodes blacklist

### `POST /api/episodes/blacklist`

**Summary:** Add an episodes subtitles to blacklist

**Operation ID:** `post_episodes_blacklist`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `seriesid` | query | integer | Yes | Series ID |
| `episodeid` | query | integer | Yes | Episode ID |
| `provider` | query | string | Yes | Provider name |
| `subs_id` | query | string | Yes | Subtitles ID |
| `language` | query | string | Yes | Subtitles language |
| `subtitles_path` | query | string | Yes | Subtitles file path |

**Responses**

- `200`: Success
- `401`: Not Authenticated
- `404`: Episode not found
- `500`: Subtitles file not found or permission issue.

---

### `DELETE /api/episodes/blacklist`

**Summary:** Delete an episodes subtitles from blacklist

**Operation ID:** `delete_episodes_blacklist`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `all` | query | string | No | Empty episodes subtitles blacklist |
| `provider` | query | string | No | Provider name |
| `subs_id` | query | string | No | Subtitles ID |

**Responses**

- `204`: Success
- `401`: Not Authenticated

---

### `GET /api/episodes/blacklist`

**Summary:** List blacklisted episodes subtitles

**Operation ID:** `get_episodes_blacklist`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `start` | query | integer | No | Paging start integer (default: `0`) |
| `length` | query | integer | No | Paging length integer (default: `-1`) |

**Responses**

- `401`: Not Authenticated

---

## Episodes History

> List episodes history events

### `GET /api/episodes/history`

**Summary:** List episodes history events

**Operation ID:** `get_episodes_history`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `start` | query | integer | No | Paging start integer (default: `0`) |
| `length` | query | integer | No | Paging length integer (default: `-1`) |
| `episodeid` | query | integer | No | Episode ID |

**Responses**

- `401`: Not Authenticated

---

## Episodes Subtitles

> Download, upload or delete episodes subtitles

### `POST /api/episodes/subtitles`

**Summary:** Upload an episode subtitles

**Operation ID:** `post_episodes_subtitles`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `seriesid` | query | integer | Yes | Series ID |
| `episodeid` | query | integer | Yes | Episode ID |
| `language` | query | string | Yes | Language code2 |
| `forced` | query | string | Yes | Forced true/false as string |
| `hi` | query | string | Yes | HI true/false as string |
| `file` | formData | file | Yes | Subtitles file as file upload object |

**Form Data:**

| Name | Type | Required | Description |
|---|---|---|---|
| `file` | file | Yes | Subtitles file as file upload object |

**Responses**

- `204`: Success
- `401`: Not Authenticated
- `404`: Episode not found
- `409`: Unable to save subtitles file. Permission or path mapping issue?
- `500`: Episode file not found. Path mapping issue?

---

### `DELETE /api/episodes/subtitles`

**Summary:** Delete an episode subtitles

**Operation ID:** `delete_episodes_subtitles`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `seriesid` | query | integer | Yes | Series ID |
| `episodeid` | query | integer | Yes | Episode ID |
| `language` | query | string | Yes | Language code2 |
| `forced` | query | string | Yes | Forced true/false as string |
| `hi` | query | string | Yes | HI true/false as string |
| `path` | query | string | Yes | Path of the subtitles file |

**Responses**

- `204`: Success
- `401`: Not Authenticated
- `404`: Episode not found
- `500`: Subtitles file not found or permission issue.

---

### `PATCH /api/episodes/subtitles`

**Summary:** Download an episode subtitles

**Operation ID:** `patch_episodes_subtitles`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `seriesid` | query | integer | Yes | Series ID |
| `episodeid` | query | integer | Yes | Episode ID |
| `language` | query | string | Yes | Language code2 |
| `forced` | query | string | Yes | Forced true/false as string |
| `hi` | query | string | Yes | HI true/false as string |

**Responses**

- `204`: Success
- `401`: Not Authenticated
- `404`: Episode not found
- `409`: Unable to save subtitles file. Permission or path mapping issue?
- `500`: Custom error messages

---

## Episodes Wanted

> List episodes wanted subtitles

### `GET /api/episodes/wanted`

**Summary:** List episodes wanted subtitles

**Operation ID:** `get_episodes_wanted`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `start` | query | integer | No | Paging start integer (default: `0`) |
| `length` | query | integer | No | Paging length integer (default: `-1`) |
| `episodeid[]` | query | array<integer> | No | Episodes ID to list (default: `[]`) |

**Responses**

- `401`: Not Authenticated

---

## Files Browser for Bazarr

> Browse content of file system as seen by Bazarr

### `GET /api/files`

**Summary:** List Bazarr file system content

**Operation ID:** `get_browse_bazarr_fs`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `path` | query | string | No | Path to browse (default: `""`) |

**Responses**

- `401`: Not Authenticated

---

## Files Browser for Radarr

> Browse content of file system as seen by Radarr

### `GET /api/files/radarr`

**Summary:** List Radarr file system content

**Operation ID:** `get_browse_radarr_fs`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `path` | query | string | No | Path to browse (default: `""`) |

**Responses**

- `401`: Not Authenticated

---

## Files Browser for Sonarr

> Browse content of file system as seen by Sonarr

### `GET /api/files/sonarr`

**Summary:** List Sonarr file system content

**Operation ID:** `get_browse_sonarr_fs`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `path` | query | string | No | Path to browse (default: `""`) |

**Responses**

- `401`: Not Authenticated

---

## History Statistics

> Get history statistics

### `GET /api/history/stats`

**Summary:** Get history statistics

**Operation ID:** `get_history_stats`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `timeFrame` | query | string | No | Timeframe to get stats for. Must be in ["week", "month", "trimester", "year"] (default: `"month"`) |
| `action` | query | string | No | Action type to filter for. (default: `"All"`) |
| `provider` | query | string | No | Provider name to filter for. (default: `"All"`) |
| `language` | query | string | No | Language name to filter for (default: `"All"`) |

**Responses**

- `401`: Not Authenticated

---

## Movies

> List movies metadata, update movie languages profile or run actions for specific movies.

### `POST /api/movies`

**Summary:** Update specific movies languages profile

**Operation ID:** `post_movies`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `radarrid` | query | array<integer> | No | Radarr movie(s) ID (default: `[]`) |
| `profileid` | query | array<string> | No | Languages profile(s) ID or "none" (default: `[]`) |

**Responses**

- `204`: Success
- `401`: Not Authenticated
- `404`: Languages profile not found

---

### `PATCH /api/movies`

**Summary:** Run actions on specific movies

**Operation ID:** `patch_movies`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `radarrid` | query | integer | No | Radarr movie ID |
| `action` | query | string | No | Action to perform from ["scan-disk", "search-missing", "search-wanted", "sync"] |

**Responses**

- `204`: Success
- `400`: Unknown action
- `401`: Not Authenticated
- `500`: Movie file not found. Path mapping issue?

---

### `GET /api/movies`

**Summary:** List movies metadata for specific movies

**Operation ID:** `get_movies`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `start` | query | integer | No | Paging start integer (default: `0`) |
| `length` | query | integer | No | Paging length integer (default: `-1`) |
| `radarrid[]` | query | array<integer> | No | Movies IDs to get metadata for (default: `[]`) |

**Responses**

- `200`: Success
- `401`: Not Authenticated

---

## Movies Blacklist

> List, add or remove subtitles to or from movies blacklist

### `POST /api/movies/blacklist`

**Summary:** Add a movies subtitles to blacklist

**Operation ID:** `post_movies_blacklist`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `radarrid` | query | integer | Yes | Radarr ID |
| `provider` | query | string | Yes | Provider name |
| `subs_id` | query | string | Yes | Subtitles ID |
| `language` | query | string | Yes | Subtitles language |
| `subtitles_path` | query | string | Yes | Subtitles file path |

**Responses**

- `200`: Success
- `401`: Not Authenticated
- `404`: Movie not found
- `500`: Subtitles file not found or permission issue.

---

### `DELETE /api/movies/blacklist`

**Summary:** Delete a movies subtitles from blacklist

**Operation ID:** `delete_movies_blacklist`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `all` | query | string | No | Empty movies subtitles blacklist |
| `provider` | query | string | No | Provider name |
| `subs_id` | query | string | No | Subtitles ID |

**Responses**

- `204`: Success
- `401`: Not Authenticated

---

### `GET /api/movies/blacklist`

**Summary:** List blacklisted movies subtitles

**Operation ID:** `get_movies_blacklist`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `start` | query | integer | No | Paging start integer (default: `0`) |
| `length` | query | integer | No | Paging length integer (default: `-1`) |

**Responses**

- `401`: Not Authenticated

---

## Movies History

> List movies history events

### `GET /api/movies/history`

**Summary:** List movies history events

**Operation ID:** `get_movies_history`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `start` | query | integer | No | Paging start integer (default: `0`) |
| `length` | query | integer | No | Paging length integer (default: `-1`) |
| `radarrid` | query | integer | No | Movie ID |

**Responses**

- `401`: Not Authenticated

---

## Movies Subtitles

> Download, upload or delete movies subtitles

### `POST /api/movies/subtitles`

**Summary:** Upload a movie subtitles

**Operation ID:** `post_movies_subtitles`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `radarrid` | query | integer | Yes | Movie ID |
| `language` | query | string | Yes | Language code2 |
| `forced` | query | string | Yes | Forced true/false as string |
| `hi` | query | string | Yes | HI true/false as string |
| `file` | formData | file | Yes | Subtitles file as file upload object |

**Form Data:**

| Name | Type | Required | Description |
|---|---|---|---|
| `file` | file | Yes | Subtitles file as file upload object |

**Responses**

- `204`: Success
- `401`: Not Authenticated
- `404`: Movie not found
- `409`: Unable to save subtitles file. Permission or path mapping issue?
- `500`: Movie file not found. Path mapping issue?

---

### `DELETE /api/movies/subtitles`

**Summary:** Delete a movie subtitles

**Operation ID:** `delete_movies_subtitles`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `radarrid` | query | integer | Yes | Movie ID |
| `language` | query | string | Yes | Language code2 |
| `forced` | query | string | Yes | Forced true/false as string |
| `hi` | query | string | Yes | HI true/false as string |
| `path` | query | string | Yes | Path of the subtitles file |

**Responses**

- `204`: Success
- `401`: Not Authenticated
- `404`: Movie not found
- `500`: Subtitles file not found or permission issue.

---

### `PATCH /api/movies/subtitles`

**Summary:** Download a movie subtitles

**Operation ID:** `patch_movies_subtitles`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `radarrid` | query | integer | Yes | Movie ID |
| `language` | query | string | Yes | Language code2 |
| `forced` | query | string | Yes | Forced true/false as string |
| `hi` | query | string | Yes | HI true/false as string |

**Responses**

- `204`: Success
- `401`: Not Authenticated
- `404`: Movie not found
- `409`: Unable to save subtitles file. Permission or path mapping issue?
- `500`: Custom error messages

---

## Movies Wanted

> List movies wanted subtitles

### `GET /api/movies/wanted`

**Summary:** List movies wanted subtitles

**Operation ID:** `get_movies_wanted`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `start` | query | integer | No | Paging start integer (default: `0`) |
| `length` | query | integer | No | Paging length integer (default: `-1`) |
| `radarrid[]` | query | array<integer> | No | Movies ID to list (default: `[]`) |

**Responses**

- `401`: Not Authenticated

---

## Providers

> Get and reset providers status

### `POST /api/providers`

**Summary:** Reset providers status

**Operation ID:** `post_providers`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `action` | query | string | Yes | Action to perform from ["reset"] |

**Responses**

- `204`: Success
- `400`: Unknown action
- `401`: Not Authenticated

---

### `GET /api/providers`

**Summary:** Get providers status

**Operation ID:** `get_providers`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `history` | query | string | No | Provider name for history stats |

**Responses**

- `200`: Success
- `401`: Not Authenticated

---

## Providers Episodes

> List and download episodes subtitles manually

### `POST /api/providers/episodes`

**Summary:** Manually download an episode subtitles

**Operation ID:** `post_provider_episodes`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `seriesid` | query | integer | Yes | Series ID |
| `episodeid` | query | integer | Yes | Episode ID |
| `hi` | query | string | Yes | HI subtitles from ["True", "False"] |
| `forced` | query | string | Yes | Forced subtitles from ["True", "False"] |
| `original_format` | query | string | Yes | Use original subtitles format from ["True", "False"] |
| `provider` | query | string | Yes | Provider name |
| `subtitle` | query | string | Yes | Pickled subtitles as return by GET |

**Responses**

- `204`: Success
- `401`: Not Authenticated
- `404`: Episode not found
- `500`: Custom error messages

---

### `GET /api/providers/episodes`

**Summary:** Search manually for an episode subtitles

**Operation ID:** `get_provider_episodes`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `episodeid` | query | integer | Yes | Episode ID |

**Responses**

- `401`: Not Authenticated
- `404`: Episode not found
- `500`: Custom error messages

---

## Providers Movies

> List and download movies subtitles manually

### `POST /api/providers/movies`

**Summary:** Manually download a movie subtitles

**Operation ID:** `post_provider_movies`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `radarrid` | query | integer | Yes | Movie ID |
| `hi` | query | string | Yes | HI subtitles from ["True", "False"] |
| `forced` | query | string | Yes | Forced subtitles from ["True", "False"] |
| `original_format` | query | string | Yes | Use original subtitles format from ["True", "False"] |
| `provider` | query | string | Yes | Provider name |
| `subtitle` | query | string | Yes | Pickled subtitles as return by GET |

**Responses**

- `204`: Success
- `401`: Not Authenticated
- `404`: Movie not found
- `500`: Custom error messages

---

### `GET /api/providers/movies`

**Summary:** Search manually for a movie subtitles

**Operation ID:** `get_provider_movies`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `radarrid` | query | integer | Yes | Movie ID |

**Responses**

- `401`: Not Authenticated
- `404`: Movie not found
- `500`: Custom error messages

---

## Series

> List series metadata, update series languages profile or run actions for specific series.

### `POST /api/series`

**Summary:** Update specific series languages profile

**Operation ID:** `post_series`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `seriesid` | query | array<integer> | No | Sonarr series ID (default: `[]`) |
| `profileid` | query | array<string> | No | Languages profile(s) ID or "none" (default: `[]`) |

**Responses**

- `204`: Success
- `401`: Not Authenticated
- `404`: Languages profile not found

---

### `PATCH /api/series`

**Summary:** Run actions on specific series

**Operation ID:** `patch_series`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `seriesid` | query | integer | No | Sonarr series ID |
| `action` | query | string | No | Action to perform from ["scan-disk", "search-missing", "search-wanted", "sync"] |

**Responses**

- `204`: Success
- `400`: Unknown action
- `401`: Not Authenticated
- `500`: Series directory not found. Path mapping issue?

---

### `GET /api/series`

**Summary:** List series metadata for specific series

**Operation ID:** `get_series`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `start` | query | integer | No | Paging start integer (default: `0`) |
| `length` | query | integer | No | Paging length integer (default: `-1`) |
| `seriesid[]` | query | array<integer> | No | Series IDs to get metadata for (default: `[]`) |

**Responses**

- `200`: Success
- `401`: Not Authenticated

---

## Subtitles

> Apply mods/tools on external subtitles

### `PATCH /api/subtitles`

**Summary:** Apply mods/tools on external subtitles

**Operation ID:** `patch_subtitles`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `action` | query | string | Yes | Action from ["sync", "translate" or mods name] |
| `language` | query | string | Yes | Language code2 |
| `path` | query | string | Yes | Subtitles file path |
| `type` | query | string | Yes | Media type from ["episode", "movie"] |
| `id` | query | integer | Yes | Media ID (episodeId, radarrId) |
| `forced` | query | string | No | Forced subtitles from ["True", "False"] |
| `hi` | query | string | No | HI subtitles from ["True", "False"] |
| `original_format` | query | string | No | Use original subtitles format from ["True", "False"] |
| `reference` | query | string | No | Reference to use for sync from video file track number (a:0) or some subtitles file path |
| `max_offset_seconds` | query | string | No | Maximum offset seconds to allow |
| `no_fix_framerate` | query | string | No | Don't try to fix framerate from ["True", "False"] |
| `gss` | query | string | No | Use Golden-Section Search from ["True", "False"] |

**Responses**

- `204`: Success
- `401`: Not Authenticated
- `404`: Episode/movie not found
- `409`: Unable to edit subtitles file. Check logs.
- `500`: Subtitles file not found. Path mapping issue?

---

### `GET /api/subtitles`

**Summary:** Return available audio and embedded subtitles tracks with external subtitles

**Description:** Used for manual subsync modal

**Operation ID:** `get_subtitles`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `subtitlesPath` | query | string | Yes | External subtitles file path |
| `sonarrEpisodeId` | query | integer | No | Sonarr Episode ID |
| `radarrMovieId` | query | integer | No | Radarr Movie ID |

**Responses**

- `200`: Success
- `401`: Not Authenticated

---

## Subtitles Info

> Guess season number, episode number or language from uploaded subtitles filename

### `GET /api/subtitles/info`

**Summary:** Guessit over subtitles filename

**Operation ID:** `get_subtitle_name_info`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `filenames[]` | query | array<string> | Yes | Subtitles filenames |

**Responses**

- `200`: Success
- `401`: Not Authenticated

---

## System Announcements

> List announcements relative to Bazarr

### `POST /api/system/announcements`

**Summary:** Mark announcement as dismissed

**Operation ID:** `post_system_announcements`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `hash` | query | string | Yes | hash of the announcement to dismiss |

**Responses**

- `204`: Success
- `401`: Not Authenticated

---

### `GET /api/system/announcements`

**Summary:** List announcements relative to Bazarr

**Operation ID:** `get_system_announcements`

**Parameters:**

_No parameters_

**Responses**

- `200`: Success
- `401`: Not Authenticated

---

## System Backups

> List, create, restore or delete backups

### `POST /api/system/backups`

**Summary:** Create a new backup

**Operation ID:** `post_system_backups`

**Parameters:**

_No parameters_

**Responses**

- `204`: Success
- `401`: Not Authenticated

---

### `DELETE /api/system/backups`

**Summary:** Delete a backup file

**Operation ID:** `delete_system_backups`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `filename` | query | string | Yes | Backups to delete filename |

**Responses**

- `204`: Success
- `400`: Filename not provided
- `401`: Not Authenticated

---

### `PATCH /api/system/backups`

**Summary:** Restore a backup file

**Operation ID:** `patch_system_backups`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `filename` | query | string | Yes | Backups to restore filename |

**Responses**

- `204`: Success
- `400`: Filename not provided
- `401`: Not Authenticated
- `500`: Error while restoring backup. Check logs.

---

### `GET /api/system/backups`

**Summary:** List backup files

**Operation ID:** `get_system_backups`

**Parameters:**

_No parameters_

**Responses**

- `204`: Success
- `401`: Not Authenticated

---

## System Health

> List health issues

### `GET /api/system/health`

**Summary:** List health issues

**Operation ID:** `get_system_health`

**Parameters:**

_No parameters_

**Responses**

- `200`: Success
- `401`: Not Authenticated

---

## System Languages

> Get languages list

### `GET /api/system/languages`

**Summary:** List languages for history filter or for language filter menu

**Operation ID:** `get_languages`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `history` | query | string | No | Language name for history stats |

**Responses**

- `200`: Success
- `401`: Not Authenticated

---

## System Languages Profiles

> List languages profiles

### `GET /api/system/languages/profiles`

**Summary:** List languages profiles

**Operation ID:** `get_languages_profiles`

**Parameters:**

_No parameters_

**Responses**

- `200`: Success
- `401`: Not Authenticated

---

## System Logs

> List log file entries or empty log file

### `DELETE /api/system/logs`

**Summary:** Force log rotation and create a new log file

**Operation ID:** `delete_system_logs`

**Parameters:**

_No parameters_

**Responses**

- `204`: Success
- `401`: Not Authenticated

---

### `GET /api/system/logs`

**Summary:** List log entries

**Operation ID:** `get_system_logs`

**Parameters:**

_No parameters_

**Responses**

- `200`: Success
- `401`: Not Authenticated

---

## System Ping

> Unauthenticated endpoint to check Bazarr availability

### `GET /api/system/ping`

**Summary:** Return status and http 200

**Operation ID:** `get_system_ping`

**Parameters:**

_No parameters_

**Responses**

- `200`: Success

---

## System Releases

> List Bazarr releases from Github

### `GET /api/system/releases`

**Summary:** Get Bazarr releases

**Operation ID:** `get_system_releases`

**Parameters:**

_No parameters_

**Responses**

- `200`: Success
- `401`: Not Authenticated

---

## System Searches

> Search for series or movies by name

### `GET /api/system/searches`

**Summary:** List results from query

**Operation ID:** `get_searches`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `query` | query | string | Yes | Series or movie name to search for |

**Responses**

- `200`: Success
- `401`: Not Authenticated

---

## systemSettings

> System settings API endpoint

### `POST /api/system/webhooks/test`

**Summary:** Test external webhook connection

**Operation ID:** `post_system_webhook_test`

**Parameters:**

_No parameters_

**Responses**

- `200`: Success

---

## System Status

> List environment information and versions

### `GET /api/system/status`

**Summary:** Return environment information and versions

**Operation ID:** `get_system_status`

**Parameters:**

_No parameters_

**Responses**

- `200`: Success
- `401`: Not Authenticated

---

## System Tasks

> List or execute tasks

### `POST /api/system/tasks`

**Summary:** Run task

**Operation ID:** `post_system_tasks`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `taskid` | query | string | Yes | Task id of the task to run |

**Responses**

- `204`: Success
- `401`: Not Authenticated

---

### `GET /api/system/tasks`

**Summary:** List tasks

**Operation ID:** `get_system_tasks`

**Parameters:**

_No parameters_

**Responses**

- `200`: Success
- `401`: Not Authenticated

---

## System Jobs

> List, force start, move or delete jobs from the queue

### `POST /api/system/jobs`

**Summary:** Force start, move to top or move to bottom of the queue a specific job

**Operation ID:** `post_system_jobs`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `id` | query | integer | Yes | Job ID act onto |
| `action` | query | string | Yes | Action to perform from ["force_start", "move_top", "move_bottom"] |

**Responses**

- `204`: Success
- `401`: Not Authenticated

---

### `DELETE /api/system/jobs`

**Summary:** Delete a job from the queue

**Operation ID:** `delete_system_jobs`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `id` | query | integer | Yes | Job ID to delete from queue |

**Responses**

- `204`: Success
- `400`: Job ID not provided
- `401`: Not Authenticated

---

### `PATCH /api/system/jobs`

**Summary:** Empty a specific jobs queue

**Operation ID:** `patch_system_jobs`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `queueName` | query | "pending" | "failed" | "completed" | Yes | Jobs queue name to empty Enum: `pending`, `failed`, `completed` |

**Responses**

- `204`: Success
- `400`: Jobs queue name not provided
- `401`: Not Authenticated

---

### `GET /api/system/jobs`

**Summary:** List jobs from the queue

**Operation ID:** `get_system_jobs`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `id` | query | integer | No | Job ID to return |
| `status` | query | "pending" | "running" | "failed" | "completed" | No | Job status to return Enum: `pending`, `running`, `failed`, `completed` |

**Responses**

- `204`: Success
- `401`: Not Authenticated

---

## Webhooks Plex

> Webhooks endpoint that can be configured in Plex to trigger a subtitles search when playback start.

### `POST /api/webhooks/plex`

**Summary:** Trigger subtitles search on play media event in Plex

**Operation ID:** `post_web_hooks_plex`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `payload` | query | string | Yes | Webhook payload |

**Responses**

- `200`: Success
- `204`: Unhandled event or no processable data
- `400`: Bad request - missing required data
- `401`: Not Authenticated
- `404`: IMDB series/movie ID not found
- `500`: Internal server error

---

## Webhooks Radarr

> Webhooks to trigger subtitles search based on Radarr webhooks

### `POST /api/webhooks/radarr`

**Summary:** Search for missing subtitles based on Radarr webhooks

**Operation ID:** `post_web_hooks_radarr`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `payload` | body | RadarrWebhook | Yes |  |

**Request Body** (`payload`): `RadarrWebhook`

**Responses**

- `200`: Success
- `401`: Not Authenticated

---

## Webhooks Sonarr

> Webhooks to trigger subtitles search based on Sonarr webhooks

### `POST /api/webhooks/sonarr`

**Summary:** Search for missing subtitles based on Sonarr webhooks

**Operation ID:** `post_web_hooks_sonarr`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `payload` | body | SonarrWebhook | Yes |  |

**Request Body** (`payload`): `SonarrWebhook`

**Responses**

- `200`: Success
- `401`: Not Authenticated

---

## Plex Authentication

> Plex OAuth and server management

### `POST /api/plex/apikey`

**Operation ID:** `post_plex_api_key`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `apikey` | query | string | Yes | API key |

**Responses**

- `200`: Success

---

### `GET /api/plex/autopulse/config`

**Operation ID:** `get_plex_autopulse_config`

**Parameters:**

_No parameters_

**Responses**

- `200`: Success

---

### `POST /api/plex/encrypt-apikey`

**Operation ID:** `post_plex_encrypt_api_key`

**Parameters:**

_No parameters_

**Responses**

- `200`: Success

---

### `GET /api/plex/oauth/libraries`

**Operation ID:** `get_plex_libraries`

**Parameters:**

_No parameters_

**Responses**

- `200`: Success

---

### `POST /api/plex/oauth/logout`

**Operation ID:** `post_plex_logout`

**Parameters:**

_No parameters_

**Responses**

- `200`: Success

---

### `POST /api/plex/oauth/pin`

**Operation ID:** `post_plex_pin`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `clientId` | query | string | No | Client ID |

**Responses**

- `200`: Success

---

### `GET /api/plex/oauth/pin`

**Operation ID:** `get_plex_pin`

**Parameters:**

_No parameters_

**Responses**

- `200`: Success

---

### `GET /api/plex/oauth/pin/{pin_id}/check`

**Operation ID:** `get_plex_pin_check`

**Parameters:**

_No parameters_

**Responses**

- `200`: Success

---

### `GET /api/plex/oauth/servers`

**Operation ID:** `get_plex_servers`

**Parameters:**

_No parameters_

**Responses**

- `200`: Success

---

### `GET /api/plex/oauth/validate`

**Operation ID:** `get_plex_validate`

**Parameters:**

_No parameters_

**Responses**

- `200`: Success

---

### `POST /api/plex/select-server`

**Operation ID:** `post_plex_select_server`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `machineIdentifier` | query | string | Yes | Machine identifier |
| `name` | query | string | Yes | Server name |
| `uri` | query | string | Yes | Connection URI |
| `local` | query | string | No | Is local connection (default: `"false"`) |
| `payload` | body | object | Yes |  |

**Request Body** (`payload`): `object`

**Responses**

- `200`: Success

---

### `GET /api/plex/select-server`

**Operation ID:** `get_plex_select_server`

**Parameters:**

_No parameters_

**Responses**

- `200`: Success

---

### `POST /api/plex/test-connection`

**Operation ID:** `post_plex_test_connection`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `uri` | query | string | Yes | Server URI |

**Responses**

- `200`: Success

---

### `GET /api/plex/test-connection`

**Operation ID:** `get_plex_test_connection`

**Parameters:**

_No parameters_

**Responses**

- `200`: Success

---

### `POST /api/plex/webhook/create`

**Operation ID:** `post_plex_webhook_create`

**Parameters:**

_No parameters_

**Responses**

- `200`: Success

---

### `POST /api/plex/webhook/delete`

**Operation ID:** `post_plex_webhook_delete`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `webhook_url` | query | string | Yes | Webhook URL to delete |

**Responses**

- `200`: Success

---

### `GET /api/plex/webhook/list`

**Operation ID:** `get_plex_webhook_list`

**Parameters:**

_No parameters_

**Responses**

- `200`: Success

---

## Other

### `PARAMETERS /api/plex/oauth/pin/{pin_id}/check`

**Parameters:**

_No parameters_

---

## Models / Definitions

### RadarrWebhook

| Property | Type | Required | Description |
|---|---|---|---|
| `eventType` | string | Yes | Type of Radarr event (e.g. MovieAdded, Test, etc) |
| `movieFile` | RadarrMovieFile | No | Radarr movie file payload. Required for anything other than test hooks |
| `movie` | RadarrMovie | No | Radarr movie payload. Can be used to sync movies from Radarr if not found in Bazarr |

---

### RadarrMovieFile

| Property | Type | Required | Description |
|---|---|---|---|
| `id` | integer | Yes | Movie file ID |

---

### RadarrMovie

| Property | Type | Required | Description |
|---|---|---|---|
| `id` | integer | Yes | Movie ID |

---

### SonarrWebhook

| Property | Type | Required | Description |
|---|---|---|---|
| `episodes` | array<SonarrEpisode> | No | List of episodes. Can be used to sync episodes from Sonarr if not found in Bazarr. |
| `episodeFiles` | array<SonarrEpisodeFile> | No | List of episode files; required for anything other than test hooks |
| `eventType` | string | Yes | Type of Sonarr event (e.g. Test, Download, etc.) |

---

### SonarrEpisode

| Property | Type | Required | Description |
|---|---|---|---|
| `id` | integer | Yes | Episode ID |

---

### SonarrEpisodeFile

| Property | Type | Required | Description |
|---|---|---|---|
| `id` | integer | Yes | Episode file ID |

---

