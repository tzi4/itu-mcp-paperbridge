# PaperBridge setup

PaperBridge is bundled with this fork. It runs in the same process as ITU MCP;
there is no second server connection and no LLM API call. Every installation
uses its owner's university access and local document archive.

## What is enabled by default?

| Capability | Fresh installation |
| --- | --- |
| Research tools and DOI resolution | Available in the MCP tool registry |
| Crossref metadata discovery | Available when requested; search is not a download |
| Combined Crossref / OpenAlex discovery | Available with `papers_search(discovery="combined")`; bounded results with per-source status |
| Official research-resource directory | Available through `papers_resources`; entry links do not verify article access |
| Subscription literature discovery | Experimental, disabled until `PAPERBRIDGE_LIBRARY_SEARCH=1` and a managed browser are configured |
| Unpaywall discovery | Requires your own contact email |
| Local archived text and PDF pages | Available for documents with recorded external-model permission |
| IEEE / Springer browser retrieval | Disabled until a browser engine is configured |
| Publisher login | Requires configured retrieval, your own credentials and permitted document processing |
| Automatic page attachment | Requested per call; depends on the client's MCP Apps support |
| Synthetic visual diagnostic | Disabled unless `PAPERBRIDGE_VISUAL_PROBE=1` |

Installing the Python package does not download a browser, sign in to a
publisher, grant document permissions or copy the maintainer's archive.
`papers_status` describes configuration without making network requests or
testing subscription entitlement.

## Private storage

The ITU MCP adapter defaults to a `papers` directory within `NINOVA_STATE_DIR`
(normally `~/.ninova_state`). To give the standalone CLI and the integrated
tools the same archive, configure an explicit absolute `PAPERBRIDGE_DATA_DIR`
in both environments. The standalone CLI otherwise defaults to
`~/.local/share/paperbridge`.

Protect that directory using your OS account and file permissions. It contains
original documents and extracted text in a private SQLite archive. Keep it out
of Git, public shares and backups intended for distribution. This is a
single-user application, not a shared multi-user service.

## Finding research material

The tool router indexes these operations with English and Turkish search aliases.
Ordinary paper queries lead to `papers_search`, which exposes discovery sources
and filters. `papers_resources` finds database entry points,
`papers_library_query` searches ProQuest records, and `papers_resolve` looks up
access links for a known DOI. The `search` and `fetch` compatibility tools remain
available to research clients. Library catalog searches use `library_search`.

`papers_search` can combine complementary metadata indexes using
`discovery="combined"`. Select a year range with `year_from` and `year_to`,
a publication `kind`, and `order="relevance"`, `"recent"` or `"citations"`.
DOIs are normalized and deduplicated. Results identify their sources, and
individual source failures remain visible. Citation counts come from different
indexes and are not directly comparable. Ordering and filtering cover bounded
source windows, so results are not an exhaustive literature review. Existing
calls retain Crossref discovery, and `local_only=true` contacts no index.

`papers_resources` searches the official ITU Library directory by topic or
subject. It returns resource descriptions and public/institutional entry links.
A directory entry does not establish entitlement to a particular paper, enable
a publisher adapter, or grant permission to process its contents.

Search results keep discovery titles and their provenance when a matching
authorized archive entry is available. The archive label appears separately as
`archive_title`. Text responses include `metadata.delivery` for the current
call: archive reads report `network_retrieval=false`; provider delivery leaves
that field unknown rather than assuming what a custom provider downloaded.

`papers_library_query` uses the configured institutional browser for an
operator-enabled subscription search. The current experimental route targets
ProQuest records. Results are titles, dates, authors, snippets and any DOI
actually present. They are metadata, not downloaded documents. Year and
peer-review filters apply conservatively to the first results page. The parser
reads a bounded HTML response from the authenticated browser session. Unsupported
page layouts and human checks produce explicit failures. Authenticated results
must be verified in the operator's actual account before relying on this route.
The stream deadline is checked between reads. Stalled browser commands remain
subject to the shared browser runner's timeout and busy-session handling.

The subscription route stores its request budget and short-lived result cache
in the private archive. The UTC budget is 30 submitted searches per day, with
at least 15 seconds between submissions. Counts survive restarts and are shared
across processes using that archive. Cached answers do not submit another
search. Credentials remain server-side and are never tool parameters.

## Optional institutional retrieval

1. Install a matching browser from the application's virtual environment:

   ```bash
   python -m playwright install chromium
   ```

2. Set `PAPERBRIDGE_BROWSER_ENGINE=chromium` in the MCP process's environment.
   Configure your own `NINOVA_USERNAME` and `NINOVA_PASSWORD` privately. Linux
   hosts may require additional browser system dependencies. Headless mode is
   the default. `PAPERBRIDGE_BROWSER_HEADLESS=0` requires an operator-provided
   display on the server.
3. Record actual processing and external-model permission for each relevant
   document, or an applicable provider policy. This records existing rights;
   it does not create a license or establish subscription entitlement.
4. Restart or reconnect your MCP process. Check `papers_status`, then request
   an applicable paper through `papers_get_text`. Check the returned status;
   successful login alone is not proof of a verified PDF retrieval.

Document records belong in `publisher-rights.json` within the private archive,
keyed by a normalized DOI. For example, adapt this record only when you actually
have the stated permissions:

```json
{
  "10.1234/example": {
    "local_processing": true,
    "external_model": true,
    "basis": "Replace with the actual permission or license basis for this document"
  }
}
```

An explicit document denial overrides the optional `publisher-policy.json`
provider record. No permission file is supplied by default, and MCP tools cannot
create these records. Permission is rechecked for text and page reads.

IEEE and Springer Nature Link adapters are experimental. Elsevier and Wiley
retrieval are unimplemented.

## ITU OpenAthens

ITU reaches most subscribed publishers through OpenAthens, which signs in with the
library account: your student or staff number and the library catalog password, not
the central ITU password. Set them privately to turn the OpenAthens adapter on:

```dotenv
PAPERBRIDGE_OPENATHENS_USERNAME=student-or-staff-number
PAPERBRIDGE_OPENATHENS_PASSWORD=library-account-password
```

Without these, `NINOVA_LIBRARY_ID` and `NINOVA_LIBRARY_PIN` are used, since the library
catalog login is the same account. The adapter opens the DOI through
`go.openathens.net/redirector/itu.edu.tr`, fills the form only on ITU's
`login.openathens.net` page, and reads the PDF named by the article page. Permission
records use the provider name `openathens`.

| Publisher | Observed in my tests |
| --- | --- |
| ASME Digital Collection | Works; ITU routes it through the OpenAthens proxy |
| Cambridge Core | Works from a home connection; a VPN address gets a verification page |
| ACS, Oxford Academic, Taylor & Francis, SAGE, ACM | Tried, but often stop at a verification page or refuse the PDF |

A publisher's verification page (for example Cloudflare's "Just a moment...") ends the
attempt with `publisher_interaction_required`. PaperBridge never solves or bypasses it.
ScienceDirect, Wiley, AIAA, ASCE, APS, RSC, Emerald and AIP showed that page every time. Reported open-access locations return public links
instead of starting institutional acquisition. Missing open-access metadata is
not proof of a paywall. Existing authorized archive entries remain readable.

## Important settings

| Variable | Purpose |
| --- | --- |
| `PAPERBRIDGE_DATA_DIR` | Your private archive directory |
| `PAPERBRIDGE_CONTACT_EMAIL` | Your contact email for metadata APIs; do not copy someone else's |
| `PAPERBRIDGE_OPENALEX_KEY` | Optional private API key for OpenAlex discovery; sent in an authorization header |
| `PAPERBRIDGE_LIBRARY_SEARCH` | Set to `1` to enable the experimental institutional literature search; default off |
| `PAPERBRIDGE_BROWSER_ENGINE` | `chromium` or `firefox`; unset means no managed retrieval |
| `PAPERBRIDGE_BROWSER_HEADLESS` | `1` by default; `0` requires a server display |
| `PAPERBRIDGE_CHROMIUM_EXECUTABLE` | Optional absolute path to your provisioned full Chromium |
| `PAPERBRIDGE_BROWSER_IDLE_SECONDS` | Default 90; idle browser cleanup, not a per-request deadline |
| `PAPERBRIDGE_SPRINGER_BROWSER_ENGINE` | Optional Springer override; otherwise inherits the common engine |
| `PAPERBRIDGE_PUBLISHER_PROXY` | Optional pre-existing loopback HTTP CONNECT proxy; creates no proxy or VPN |
| `PAPERBRIDGE_OPENATHENS_PUBLISHER_PROXY` | Set to `1` to use that proxy for OpenAthens browser sessions and PDF requests. Extend its allowlist first. Missing or invalid configuration fails without a direct retry. Restart the process after changing proxy settings. |
| `PAPERBRIDGE_VISUAL_PROBE` | Opt-in synthetic client image diagnostic |

Use `papers_get_text` for the relevant excerpts and follow `next_id` when more
text is needed. Select PDF pages based on the text and captions. `papers_get_page`
returns a native image. `papers_view_pages` prepares a selection for compatible
MCP Apps clients, with an 8 MB image budget. Automatic attachment is per-call
opt-in. Tool success or host acknowledgement does not establish that the model
received and inspected the pixels.

## Try the original synthetic example

From the repository root and activated virtual environment:

```bash
paperbridge --data-dir .paperbridge-demo ingest examples/paperbridge/demo-note.txt --title "PaperBridge demo" --url https://example.org/paperbridge-demo --rights-file examples/paperbridge/demo-rights.json
paperbridge --data-dir .paperbridge-demo search rocket --local-only
```

The fixture is original demonstration text, not a scientific publication. Its
permission record applies only to that fixture. Use the returned `first_chunk_id`
with `paperbridge --data-dir .paperbridge-demo fetch ID` to read it.
