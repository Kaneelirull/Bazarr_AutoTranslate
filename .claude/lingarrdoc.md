# Lingarr HTTP API Documentation

**Version:** 1.0.9  
**License:** [GNU Affero General Public License v3.0](https://github.com/lingarr-translate/lingarr/blob/main/LICENSE)  
**Description:** Lingarr HTTP API definition  

---

## Table of Contents

- [Auth](#auth)
- [Directory](#directory)
- [Image](#image)
- [Logs](#logs)
- [Mapping](#mapping)
- [Media](#media)
- [RequestTemplate](#requesttemplate)
- [Schedule](#schedule)
- [Setting](#setting)
- [Statistics](#statistics)
- [Subtitle](#subtitle)
- [Telemetry](#telemetry)
- [Translate](#translate)
- [TranslationRequest](#translationrequest)
- [Version](#version)
- [Webhook](#webhook)
- [Schemas](#schemas)

---

## Auth

### `POST /api/Auth/login`

**Summary:** Login with username and password

**Parameters:**

_No parameters_

**Request Body**

Schema: `LoginRequest`

**Responses**

- `200`: OK

---

### `POST /api/Auth/signup`

**Summary:** Create the first user (signup wizard)

**Parameters:**

_No parameters_

**Request Body**

Schema: `SignupRequest`

**Responses**

- `200`: OK

---

### `GET /api/Auth/authenticated`

**Summary:** Verify if the request is authenticated via Cookie and check onboarding status

**Parameters:**

_No parameters_

**Responses**

- `200`: OK

---

### `POST /api/Auth/logout`

**Summary:** Logout the current user

**Parameters:**

_No parameters_

**Responses**

- `200`: OK

---

### `POST /api/Auth/onboarding`

**Summary:** Complete onboarding and configure authentication

**Parameters:**

_No parameters_

**Request Body**

Schema: `OnboardingRequest`

**Responses**

- `200`: OK

---

### `POST /api/Auth/apikey/generate`

**Summary:** Generate a new API key (only during onboarding or if not already exists)

**Parameters:**

_No parameters_

**Responses**

- `200`: OK → `ApiKeyResponse`

---

### `GET /api/Auth/users/any`

**Summary:** Check if any users exist in the system

**Parameters:**

_No parameters_

**Responses**

- `200`: OK → `boolean`

---

### `GET /api/Auth/users`

**Summary:** Get all users

**Parameters:**

_No parameters_

**Responses**

- `200`: OK

---

### `PUT /api/Auth/users/{id}`

**Summary:** Update a user

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| id | path | integer(int32) | Yes |  |

**Request Body**

Schema: `UpdateUserRequest`

**Responses**

- `200`: OK

---

### `DELETE /api/Auth/users/{id}`

**Summary:** Delete a user

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| id | path | integer(int32) | Yes |  |

**Responses**

- `200`: OK

---

## Directory

### `GET /api/Directory/get`

**Summary:** Retrieves the contents of a specified directory.

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| path | query | string | No | The full path to the directory to browse. |

**Responses**

- `200`: Successfully retrieved directory contents → `array<DirectoryItem>`
- `403`: Access to directory is denied
- `404`: Directory not found at specified path
- `500`: Internal server error occurred during operation

---

## Image

### `GET /api/Image/show/{path}`

**Summary:** Retrieves images related to a TV show based on the specified path.

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| path | path | string | Yes | The API path for accessing the TV show images. This path is appended to the Sonarr API URL. |

**Responses**

- `200`: OK

---

### `GET /api/Image/movie/{path}`

**Summary:** Retrieves images related to a movie based on the specified path.

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| path | path | string | Yes | The API path for accessing the movie images. This path is appended to the Radarr API URL. |

**Responses**

- `200`: OK

---

## Logs

### `GET /api/Logs/stream`

**Parameters:**

_No parameters_

**Responses**

- `200`: OK

---

## Mapping

### `GET /api/Mapping/get`

**Summary:** Retrieves all path mappings from the system.

**Parameters:**

_No parameters_

**Responses**

- `200`: Returns the list of path mappings. → `array<PathMapping>`

---

### `POST /api/Mapping/set`

**Summary:** Updates or creates path mappings in the system.

**Parameters:**

_No parameters_

**Request Body**: The list of path mappings to set.

Schema: `array<PathMapping>`

**Responses**

- `200`: The mappings were successfully updated.
- `400`: If the mappings are invalid.

---

## Media

### `GET /api/Media/movies`

**Summary:** Retrieves a paginated list of movies based on optional search criteria and sorting parameters.

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| searchQuery | query | string | No | An optional search query to filter movies by title or other attributes. |
| orderBy | query | string | No | An optional parameter specifying the field to sort by (e.g., "Title", "DateAdded"). |
| ascending | query | boolean | No | A boolean indicating whether to sort in ascending order (default is true). |
| pageSize | query | integer(int32) | No | The number of movies to return per page (default is 20). |
| pageNumber | query | integer(int32) | No | The page number to retrieve (default is 1). |

**Responses**

- `200`: OK → `MovieResponsePagedResult`

---

### `GET /api/Media/shows`

**Summary:** Retrieves a paginated list of shows based on optional search criteria and sorting parameters.

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| searchQuery | query | string | No | An optional search query to filter shows by title or other attributes. |
| orderBy | query | string | No | An optional parameter specifying the field to sort by (e.g., "Title", "DateAdded"). |
| ascending | query | boolean | No | A boolean indicating whether to sort in ascending order (default is true). |
| pageSize | query | integer(int32) | No | The number of shows to return per page (default is 20). |
| pageNumber | query | integer(int32) | No | The page number to retrieve (default is 1). |

**Responses**

- `200`: OK → `ShowPagedResult`

---

### `POST /api/Media/exclude`

**Summary:** Toggles the exclusion status of a specified media item from translation.

**Parameters:**

_No parameters_

**Request Body**: The request object containing the media type and id.

Schema: `ExcludeRequest`

**Responses**

- `200`: OK → `ShowPagedResult`

---

### `POST /api/Media/threshold`

**Summary:** Sets the amount of hours a media file needs to exist before translation is initiated.

**Parameters:**

_No parameters_

**Request Body**: The request object containing the media type, id, and amount of hours to be set.

Schema: `ThresholdRequest`

**Responses**

- `200`: OK → `ShowPagedResult`

---

## RequestTemplate

### `GET /api/RequestTemplate/defaults`

**Summary:** Retrieves the default request templates for all AI providers.

**Parameters:**

_No parameters_

**Responses**

- `200`: OK → `object`

---

## Schedule

### `GET /api/Schedule/jobs`

**Summary:** Retrieves information about all jobs in the system.

**Parameters:**

_No parameters_

**Responses**

- `200`: OK

---

### `POST /api/Schedule/job/start`

**Parameters:**

_No parameters_

**Request Body**

Schema: `StartJobRequest`

**Responses**

- `200`: OK

---

### `GET /api/Schedule/job/automation`

**Summary:** Enqueues a background job to automate translation.

**Parameters:**

_No parameters_

**Responses**

- `200`: OK

---

### `GET /api/Schedule/job/movie`

**Summary:** Enqueues a background job to retrieve movie data.

**Parameters:**

_No parameters_

**Responses**

- `200`: OK

---

### `GET /api/Schedule/job/show`

**Summary:** Enqueues a background job to retrieve show data.

**Parameters:**

_No parameters_

**Responses**

- `200`: OK

---

### `DELETE /api/Schedule/job/remove/{jobId}`

**Summary:** Attempts to remove a background job from the queue or stop it if it's already running.

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| jobId | path | string | Yes | The unique identifier of the job to be removed. |

**Responses**

- `200`: OK → `string`

---

### `POST /api/Schedule/job/index/movies`

**Summary:** Enqueues a background job to reindex shows and movies

**Parameters:**

_No parameters_

**Responses**

- `200`: OK

---

### `POST /api/Schedule/job/index/shows`

**Summary:** Enqueues a background job to reindex shows and movies

**Parameters:**

_No parameters_

**Responses**

- `200`: OK

---

## Setting

### `GET /api/Setting/{key}`

**Summary:** Retrieves the value of a specific setting by its key.

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| key | path | string | Yes | The key of the setting to retrieve. |

**Responses**

- `200`: OK → `string`

---

### `POST /api/Setting/multiple/get`

**Summary:** Retrieves the values of multiple settings by their keys.

**Parameters:**

_No parameters_

**Request Body**: A list of keys for the settings to retrieve.

Schema: `array<string>`

**Responses**

- `200`: OK → `object`

---

### `POST /api/Setting`

**Summary:** Updates or creates a setting with the specified key and value.

**Parameters:**

_No parameters_

**Request Body**: The setting object containing the key and value to be updated or created.

Schema: `Setting`

**Responses**

- `200`: OK → `boolean`

---

### `POST /api/Setting/multiple/set`

**Summary:** Updates or creates multiple settings with the specified keys and values.

**Parameters:**

_No parameters_

**Request Body**: A dictionary where the keys are setting keys and the values are the new values to assign.

Schema: `object`

**Responses**

- `200`: OK → `boolean`

---

### `POST /api/Setting/encrypted`

**Summary:** Encrypts and stores a single sensitive setting value.

**Parameters:**

_No parameters_

**Request Body**: The setting object containing the key and plaintext value to encrypt and store.

Schema: `Setting`

**Responses**

- `200`: OK

---

### `POST /api/Setting/multiple/encrypted/get`

**Summary:** Retrieves and decrypts sensitive settings values.
Returns an empty string for keys not found or that contain a non-decryptable value.

**Parameters:**

_No parameters_

**Request Body**: A list of setting keys to retrieve and decrypt.

Schema: `array<string>`

**Responses**

- `200`: OK → `object`

---

## Statistics

### `GET /api/Statistics`

**Parameters:**

_No parameters_

**Responses**

- `200`: OK → `Statistics`

---

### `GET /api/Statistics/daily/{days}`

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| days | path | integer(int32) | Yes |  |

**Responses**

- `200`: OK → `array<DailyStatistics>`

---

### `POST /api/Statistics/reset`

**Parameters:**

_No parameters_

**Responses**

- `200`: OK

---

## Subtitle

### `POST /api/Subtitle/all`

**Summary:** Retrieves a list of subtitle files located at the specified path.

**Parameters:**

_No parameters_

**Request Body**: The directory path to search for subtitle files.This path is relative to the media folder
            and should not start with a forward slash.

Schema: `SubtitlePath`

**Responses**

- `200`: OK → `array<Subtitles>`

---

## Telemetry

### `GET /api/Telemetry/preview`

**Parameters:**

_No parameters_

**Responses**

- `200`: OK → `TelemetryPayload`

---

## Translate

### `POST /api/Translate/file`

**Summary:** Initiates a translation job for the provided subtitle data.

**Parameters:**

_No parameters_

**Request Body**: The subtitle data to be translated. 
            This includes the subtitle path, subtitle source language and subtitle target language.

Schema: `TranslateAbleSubtitle`

**Responses**

- `200`: OK → `TranslationJobDto`

---

### `POST /api/Translate/bulk`

**Summary:** Initiates translation jobs for multiple media items.
Handles subtitle discovery and source language resolution server-side.

**Parameters:**

_No parameters_

**Request Body**: The bulk translate request containing media IDs, target language, and media type.

Schema: `BulkTranslateRequest`

**Responses**

- `200`: OK

---

### `POST /api/Translate/line`

**Summary:** Translate a single subtitle line

**Parameters:**

_No parameters_

**Request Body**: The subtitle to be translated. 
            This includes the subtitle line, subtitle source language and subtitle target language.

Schema: `TranslateAbleSubtitleLine`

**Responses**

- `200`: OK → `string`

---

### `POST /api/Translate/content`

**Summary:** Translates subtitle content, supporting both single line and batch translation.

**Parameters:**

_No parameters_

**Request Body**: The translation request containing one or more subtitle items

Schema: `TranslateAbleSubtitleContent`

**Responses**

- `200`: OK → `array<BatchTranslatedLine>`

---

### `GET /api/Translate/languages`

**Summary:** Retrieves a list of available source languages and their supported target languages.

**Parameters:**

_No parameters_

**Responses**

- `200`: OK → `array<SourceLanguage>`

---

### `GET /api/Translate/models`

**Summary:** Retrieves available AI models for the currently active translation service.

**Parameters:**

_No parameters_

**Responses**

- `200`: OK → `array<LabelValue>`

---

## TranslationRequest

### `GET /api/TranslationRequest/{id}`

**Summary:** Gets a single translation request with its event timeline

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| id | path | integer(int32) | Yes | The ID of the translation request |

**Responses**

- `200`: Returns the translation request detail → `TranslationRequestDetail`
- `404`: If the translation request was not found

---

### `GET /api/TranslationRequest/active`

**Summary:** Gets the count of active translation requests

**Parameters:**

_No parameters_

**Responses**

- `200`: Returns the count of active translation requests → `integer(int32)`
- `500`: If there was an error checking for updates

---

### `GET /api/TranslationRequest/requests`

**Summary:** Retrieves a paginated list of translation requests with optional filtering and sorting

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| searchQuery | query | string | No | Optional search term to filter requests |
| orderBy | query | string | No | Property name to sort the results by |
| ascending | query | boolean | No | Sort direction; true for ascending, false for descending |
| pageSize | query | integer(int32) | No | Number of items per page |
| pageNumber | query | integer(int32) | No | Page number to retrieve |

**Responses**

- `200`: Returns the paginated list of translation requests → `TranslationRequestPagedResult`
- `500`: If there was an error checking for updates

---

### `POST /api/TranslationRequest/cancel`

**Summary:** Cancels an existing translation request

**Parameters:**

_No parameters_

**Request Body**: The translation request to cancel

Schema: `TranslationRequest`

**Responses**

- `200`: Returns the canceled translation request → `string`
- `404`: If the translation request was not found
- `500`: If there was an error checking for updates

---

### `POST /api/TranslationRequest/remove`

**Summary:** Removes an existing translation request

**Parameters:**

_No parameters_

**Request Body**: The translation request to remove

Schema: `TranslationRequest`

**Responses**

- `200`: Returns the removed translation request → `string`
- `404`: If the translation request was not found
- `500`: If there was an error checking for updates

---

### `POST /api/TranslationRequest/retry`

**Summary:** Retries an existing translation request
Does not delete the current one, just reques
The request with the same information

**Parameters:**

_No parameters_

**Request Body**: The translation request to retry

Schema: `TranslationRequest`

**Responses**

- `200`: Returns the new translation request → `string`
- `404`: If the translation request was not found
- `500`: If there was an error checking for updates

---

## Version

### `GET /api/Version`

**Summary:** Retrieves the current version information and checks for available updates.

**Parameters:**

_No parameters_

**Responses**

- `200`: Returns the version information → `VersionInfo`
- `500`: If there was an error checking for updates

---

## Webhook

### `POST /api/Webhook/radarr`

**Summary:** Receives webhook events from Radarr

**Parameters:**

_No parameters_

**Request Body**: The webhook payload from Radarr

Schema: `RadarrWebhookPayload`

**Responses**

- `200`: OK

---

### `POST /api/Webhook/sonarr`

**Summary:** Receives webhook events from Sonarr

**Parameters:**

_No parameters_

**Request Body**: The webhook payload from Sonarr

Schema: `SonarrWebhookPayload`

**Responses**

- `200`: OK

---

## Schemas

### ApiKeyResponse

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| apiKey | string | No | Yes |  |

---

### BatchSubtitleLine

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| position | integer(int32) | Yes | No | Position or index identifier of the subtitle |
| line | string | Yes | No | Line to translate |

---

### BatchTranslatedLine

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| position | integer(int32) | No | No | Position or index identifier matching the original subtitle |
| line | string | No | Yes | Translated line |

---

### BulkTranslateRequest

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| mediaIds | array<integer(int32)> | Yes | Yes |  |
| targetLanguage | string | Yes | Yes |  |
| mediaType | MediaType | Yes | No |  |

---

### DailyStatistics

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| id | integer(int32) | No | No |  |
| createdAt | string(date-time) | No | No |  |
| updatedAt | string(date-time) | No | No |  |
| date | string(date-time) | Yes | No |  |
| translationCount | integer(int32) | No | No |  |

---

### DirectoryItem

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| name | string | Yes | Yes |  |
| fullPath | string | Yes | Yes |  |

---

### Episode

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| id | integer(int32) | No | No |  |
| createdAt | string(date-time) | No | No |  |
| updatedAt | string(date-time) | No | No |  |
| sonarrId | integer(int32) | Yes | No |  |
| episodeNumber | integer(int32) | Yes | No |  |
| title | string | Yes | Yes |  |
| fileName | string | No | Yes |  |
| path | string | No | Yes |  |
| mediaHash | string | No | Yes |  |
| dateAdded | string(date-time) | No | Yes |  |
| seasonId | integer(int32) | No | No |  |
| season | Season | Yes | No |  |
| excludeFromTranslation | boolean | No | No |  |

---

### ExcludeRequest

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| mediaType | MediaType | No | No |  |
| id | integer(int32) | No | No |  |

---

### Image

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| id | integer(int32) | No | No |  |
| type | string | Yes | Yes |  |
| path | string | Yes | Yes |  |
| showId | integer(int32) | No | Yes |  |
| show | Show | No | No |  |
| movieId | integer(int32) | No | Yes |  |
| movie | Movie | No | No |  |

---

### LabelValue

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| label | string | No | Yes | Display text |
| value | string | No | Yes | Internal value |

---

### LoginRequest

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| username | string | Yes | Yes |  |
| password | string | Yes | Yes |  |

---

### MediaType

**Enum values:** `Movie` | `Show` | `Season` | `Episode`

---

### Movie

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| id | integer(int32) | No | No |  |
| createdAt | string(date-time) | No | No |  |
| updatedAt | string(date-time) | No | No |  |
| radarrId | integer(int32) | Yes | No |  |
| title | string | Yes | Yes |  |
| fileName | string | Yes | Yes |  |
| path | string | Yes | Yes |  |
| mediaHash | string | No | Yes |  |
| dateAdded | string(date-time) | Yes | Yes |  |
| images | array<Image> | No | Yes |  |
| excludeFromTranslation | boolean | No | No |  |
| translationAgeThreshold | integer(int32) | No | Yes |  |

---

### MovieResponse

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| id | integer(int32) | No | No |  |
| radarrId | integer(int32) | Yes | No |  |
| title | string | Yes | Yes |  |
| fileName | string | Yes | Yes |  |
| path | string | Yes | Yes |  |
| dateAdded | string(date-time) | Yes | Yes |  |
| images | array<Image> | No | Yes |  |
| subtitles | array<Subtitles> | No | Yes |  |
| excludeFromTranslation | boolean | No | No |  |
| translationAgeThreshold | integer(int32) | No | Yes |  |

---

### MovieResponsePagedResult

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| items | array<MovieResponse> | No | Yes |  |
| totalCount | integer(int32) | Yes | No |  |
| pageNumber | integer(int32) | Yes | No |  |
| pageSize | integer(int32) | Yes | No |  |

---

### OnboardingRequest

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| enableUserAuth | string | Yes | Yes |  |

---

### PathMapping

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| id | integer(int32) | No | No |  |
| createdAt | string(date-time) | No | No |  |
| updatedAt | string(date-time) | No | No |  |
| sourcePath | string | Yes | Yes |  |
| destinationPath | string | Yes | Yes |  |
| mediaType | MediaType | Yes | No |  |

---

### RadarrWebhookMovie

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| id | integer(int32) | No | No |  |
| title | string | No | Yes |  |

---

### RadarrWebhookPayload

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| movie | RadarrWebhookMovie | No | No |  |

---

### Season

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| id | integer(int32) | No | No |  |
| createdAt | string(date-time) | No | No |  |
| updatedAt | string(date-time) | No | No |  |
| seasonNumber | integer(int32) | Yes | No |  |
| path | string | No | Yes |  |
| episodes | array<Episode> | No | Yes |  |
| showId | integer(int32) | No | No |  |
| show | Show | Yes | No |  |
| excludeFromTranslation | boolean | No | No |  |

---

### Setting

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| key | string | Yes | Yes |  |
| value | string | Yes | Yes |  |

---

### Show

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| id | integer(int32) | No | No |  |
| createdAt | string(date-time) | No | No |  |
| updatedAt | string(date-time) | No | No |  |
| sonarrId | integer(int32) | Yes | No |  |
| title | string | Yes | Yes |  |
| path | string | Yes | Yes |  |
| dateAdded | string(date-time) | Yes | Yes |  |
| images | array<Image> | No | Yes |  |
| seasons | array<Season> | No | Yes |  |
| excludeFromTranslation | boolean | No | No |  |
| translationAgeThreshold | integer(int32) | No | Yes |  |

---

### ShowPagedResult

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| items | array<Show> | No | Yes |  |
| totalCount | integer(int32) | Yes | No |  |
| pageNumber | integer(int32) | Yes | No |  |
| pageSize | integer(int32) | Yes | No |  |

---

### SignupRequest

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| username | string | Yes | Yes |  |
| password | string | Yes | Yes |  |

---

### SonarrWebhookEpisode

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| id | integer(int32) | No | No |  |

---

### SonarrWebhookPayload

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| series | SonarrWebhookSeries | No | No |  |
| episodes | array<SonarrWebhookEpisode> | No | Yes |  |

---

### SonarrWebhookSeries

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| id | integer(int32) | No | No |  |
| title | string | No | Yes |  |

---

### SourceLanguage

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| name | string | Yes | Yes |  |
| code | string | Yes | Yes |  |
| targets | array<string> | No | Yes |  |

---

### StartJobRequest

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| jobName | string | No | Yes |  |

---

### Statistics

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| id | integer(int32) | No | No |  |
| createdAt | string(date-time) | No | No |  |
| updatedAt | string(date-time) | No | No |  |
| totalLinesTranslated | integer(int64) | No | No |  |
| totalFilesTranslated | integer(int64) | No | No |  |
| totalCharactersTranslated | integer(int64) | No | No |  |
| totalMovies | integer(int32) | No | No |  |
| totalEpisodes | integer(int32) | No | No |  |
| totalSubtitles | integer(int32) | No | No |  |
| translationsByMediaTypeJson | string | No | Yes |  |
| translationsByServiceJson | string | No | Yes |  |
| subtitlesByLanguageJson | string | No | Yes |  |
| translationsByModelJson | string | No | Yes |  |
| translationsByMediaType | object | No | Yes |  |
| translationsByService | object | No | Yes |  |
| subtitlesByLanguage | object | No | Yes |  |
| translationsByModel | object | No | Yes |  |

---

### SubtitlePath

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| path | string | Yes | Yes |  |

---

### Subtitles

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| path | string | No | Yes |  |
| fileName | string | No | Yes |  |
| language | string | No | Yes |  |
| caption | string | No | Yes |  |
| format | string | No | Yes |  |

---

### TelemetryMetrics

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| filesTranslated | integer(int64) | No | No |  |
| linesTranslated | integer(int64) | No | No |  |
| charactersTranslated | integer(int64) | No | No |  |
| serviceUsage | object | No | Yes |  |
| languagePairs | object | No | Yes |  |
| mediaTypeUsage | object | No | Yes |  |
| modelUsage | object | No | Yes |  |

---

### TelemetryPayload

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| version | string | Yes | Yes |  |
| reportDate | string | Yes | Yes |  |
| platform | string | No | Yes |  |
| metrics | TelemetryMetrics | Yes | No |  |

---

### ThresholdRequest

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| mediaType | MediaType | No | No |  |
| id | integer(int32) | No | No |  |
| hours | integer(int32) | No | No |  |

---

### TranslateAbleSubtitle

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| mediaId | integer(int32) | Yes | No |  |
| subtitlePath | string | Yes | Yes |  |
| sourceLanguage | string | Yes | Yes |  |
| targetLanguage | string | Yes | Yes |  |
| mediaType | MediaType | Yes | No |  |
| subtitleFormat | string | Yes | Yes |  |

---

### TranslateAbleSubtitleContent

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| arrMediaId | integer(int32) | Yes | No |  |
| title | string | Yes | Yes |  |
| sourceLanguage | string | Yes | Yes |  |
| targetLanguage | string | Yes | Yes |  |
| mediaType | MediaType | Yes | No |  |
| lines | array<BatchSubtitleLine> | Yes | Yes |  |

---

### TranslateAbleSubtitleLine

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| subtitleLine | string | Yes | Yes |  |
| sourceLanguage | string | Yes | Yes |  |
| targetLanguage | string | Yes | Yes |  |
| contextLinesBefore | array<string> | No | Yes |  |
| contextLinesAfter | array<string> | No | Yes |  |

---

### TranslationJobDto

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| jobId | integer(int32) | Yes | No |  |

---

### TranslationRequest

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| id | integer(int32) | No | No |  |
| createdAt | string(date-time) | No | No |  |
| updatedAt | string(date-time) | No | No |  |
| jobId | string | No | Yes |  |
| mediaId | integer(int32) | No | Yes |  |
| title | string | Yes | Yes |  |
| sourceLanguage | string | Yes | Yes |  |
| targetLanguage | string | Yes | Yes |  |
| subtitleToTranslate | string | No | Yes |  |
| translatedSubtitle | string | No | Yes |  |
| mediaType | MediaType | Yes | No |  |
| status | TranslationStatus | Yes | No |  |
| completedAt | string(date-time) | No | Yes |  |
| errorMessage | string | No | Yes |  |
| stackTrace | string | No | Yes |  |

---

### TranslationRequestDetail

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| id | integer(int32) | No | No |  |
| jobId | string | No | Yes |  |
| mediaId | integer(int32) | No | Yes |  |
| title | string | Yes | Yes |  |
| sourceLanguage | string | Yes | Yes |  |
| targetLanguage | string | Yes | Yes |  |
| subtitleToTranslate | string | No | Yes |  |
| translatedSubtitle | string | No | Yes |  |
| mediaType | MediaType | Yes | No |  |
| status | TranslationStatus | Yes | No |  |
| completedAt | string(date-time) | No | Yes |  |
| errorMessage | string | No | Yes |  |
| stackTrace | string | No | Yes |  |
| progress | integer(int32) | No | No |  |
| createdAt | string(date-time) | No | No |  |
| updatedAt | string(date-time) | No | No |  |
| events | array<TranslationRequestEventDetail> | No | Yes |  |
| lines | array<TranslationRequestSubtitleLines> | No | Yes |  |

---

### TranslationRequestEventDetail

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| id | integer(int32) | No | No |  |
| status | TranslationStatus | Yes | No |  |
| message | string | No | Yes |  |
| createdAt | string(date-time) | No | No |  |

---

### TranslationRequestPagedResult

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| items | array<TranslationRequest> | No | Yes |  |
| totalCount | integer(int32) | Yes | No |  |
| pageNumber | integer(int32) | Yes | No |  |
| pageSize | integer(int32) | Yes | No |  |

---

### TranslationRequestSubtitleLines

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| position | integer(int32) | No | No |  |
| source | string | Yes | Yes |  |
| target | string | Yes | Yes |  |

---

### TranslationStatus

**Enum values:** `Pending` | `InProgress` | `Completed` | `Failed` | `Cancelled` | `Interrupted`

---

### UpdateUserRequest

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| username | string | No | Yes |  |
| password | string | No | Yes |  |

---

### VersionInfo

| Property | Type | Required | Nullable | Description |
|---|---|---|---|---|
| newVersion | boolean | No | No |  |
| currentVersion | string | No | Yes |  |
| latestVersion | string | No | Yes |  |

---

