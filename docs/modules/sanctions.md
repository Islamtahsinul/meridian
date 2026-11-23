# Sanctions module

Core-owned, backend only. Loads four official sanctions lists, keeps their vessels and aircraft, and merges
them into one index that the ships and flights modules query in-process (CONTRACT 5.7). It has no map layer
of its own: the ships module draws `ships.sanctioned` and the flights module rings listed aircraft.

Files: `meridian/modules/sanctions/__init__.py` (index, Python API, routes, builds), `parsers.py` (one
parser per list format), `merge.py` (the merge rules). Tests: `tests/test_sanctions.py` with fixtures in
`tests/fixtures/sanctions/`.

## Sources

| List | Publisher | File | Licence or terms | Refresh |
|---|---|---|---|---|
| OFAC Specially Designated Nationals list | US Treasury, OFAC | `SDN.CSV` (CSV), a few MB | US Government work, public domain | Daily |
| UK Sanctions List | Foreign, Commonwealth and Development Office | `UK-Sanctions-List.xml` (schema 4.33), about 22 MB | Open Government Licence v3.0 | Daily; the host honours ETag and If-Modified-Since, so an unchanged list costs a 304 |
| UN Security Council consolidated list | UN Security Council | `consolidated.xml` (redirects to an Azure blob), about 2.2 MB | UN terms of use: "for the User's personal, non-commercial use, without any right to resell or redistribute them" | Daily (ETag on the blob) |
| EU consolidated financial sanctions list (FSF 1.1) | European Commission | `xmlFullSanctionsList_1_1`, 26 MB, 3.4 MB gzip | European Commission reuse notice (Decision 2011/833/EU): reuse allowed with acknowledgement of the source | Daily; the host ignores conditional requests, so the module asks for gzip |

Endpoints:
- OFAC: `https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.CSV`
- UK: `https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml` (the old OFSI Consolidated List closed on
  28 January 2026; its blob answers 409)
- UN: `https://scsanctions.un.org/resources/xml/en/consolidated.xml`
- EU: `https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw`
  (the token is the public one published with the dataset on data.europa.eu)

Each list is its own dataset (`sanctions.sdn`, `sanctions.uk`, `sanctions.un`, `sanctions.eu`, 24 hours) and
its own source (`sanctions.ofac_sdn`, `sanctions.uk`, `sanctions.un`, `sanctions.eu`). Raw files live in
`data/raw/sanctions/` (UK 22 MB, EU 3.4 MB, UN 2.2 MB plus the OFAC CSV) and builds in `data/build/sanctions/` (`sdn.json`, `uk.json`,
`un.json`, `eu.json`). `python server.py --build sanctions --only sanctions` builds them all.

## What is kept

- **OFAC**: vessels (IMO, MMSI, call sign, flag, type, tonnage, owner, aliases, linked entities) and aircraft
  (registration, Mode S, serial number, model, operator), as before.
- **UK**: designations of type Ship: IMO, names and aliases, current and previous flags, type, tonnage, length,
  year built, current and previous owners or operators, regime, date designated, sanctions imposed and the
  designation source (UK or UN). The UK schema has no aircraft type, and no MMSI or call sign.
- **UN**: the consolidated file has no vessel type. An entity counts as a vessel when its comments give a ship
  IMO number with a valid check digit, mention a flag or "the vessel", and none of its names is a company name.
  Today that is one vessel (AVAX). The UN 1718 Committee's DPRK vessel designations are a separate list; the
  UK carries them (37 ships with designation source UN).
- **EU**: the same rule, with the IMO from the structured `identification` element. Today: two vessels
  (Unica and New Konk). Shipping companies have 7-digit IMO company numbers too; those fail the check digit or
  carry company names, so they are never read as ships. EU shadow-fleet port bans sit in an annex to
  Regulation 833/2014, not in this file.
- **No list other than OFAC lists aircraft** (only aircraft companies), so aircraft stay OFAC-only for now;
  the merge handles aircraft from any list.

**People rule.** Individuals are counted, never kept: no names, dates of birth or documents of people are
stored, searched or returned. Organisations appear only as a vessel's owner, operator or linked entity.
Parsers refuse XML that declares a DOCTYPE or entities. The refusal is in the XML parser itself, so it holds for
any encoding (UTF-16 included) and however long the prolog is.

Counts on 23 November 2026: OFAC 1,570 vessels and 342 aircraft; UK 693 ships (692 with an IMO), 389 of
them not on OFAC; UN 1 vessel, EU 2 vessels, all three not on OFAC. Merged: 1,961 vessels (305 on more than
one list) and 342 aircraft. Of the 33,693 vessels in Meridian's 7-day AIS history, confirmed matches rise from
33 (OFAC) to 82; the 49 new ones are UK-listed.

## Merge rules

- Lists are taken in the order OFAC, UK, UN, EU. The first list that names a vessel or aircraft gives the
  merged record its `uid`, name and fields; later lists only fill fields that are still empty. Each list's own
  view (its id, name, programmes, dates, flag, owner) stays in `listings`.
- Vessels merge on a shared IMO number, or on a shared MMSI when exactly one record matches and the IMO
  numbers do not disagree. Aircraft merge on a shared Mode S code or registration with the same checks.
  Serial numbers repeat across aircraft types (OFAC has two pairs), so they never merge on their own.
- Records from the same list never merge with each other.
- Lookups by IMO, MMSI, registration, Mode S and serial number are exact. Name lookups cover every list's
  names and aliases and stay weak (`match == "name"`).
- `uid`: the OFAC entity number when OFAC lists the vessel or aircraft (unchanged, so favourites and case
  files keep working), otherwise `uk-<UniqueID>`, `un-<DATAID>` or `eu-<logicalId>`. `record(uid)` also
  accepts any list's own uid. A vessel that OFAC adds later changes uid from `uk-...` to the OFAC number.

## Python API (CONTRACT 5.7)

`lookup_vessel(imo=, mmsi=, name=)`, `lookup_aircraft(hex=, reg=, msn=)`, `vessels()`, `aircraft()` and
`ready()` keep their signatures and return shape. Records keep every OFAC key (`uid`, `name`, `program`,
`linked_to`, `aliases`, `built`, `remarks`, `kind`, and `imo`, `mmsi`, `call_sign`, `vessel_type`, `tonnage`,
`grt`, `flag`, `owner` for vessels or `registration`, `mode_s`, `msn`, `model`, `operator` for aircraft) and
add:

- `lists`: e.g. `["OFAC", "UK"]`, in list order.
- `programmes`: per list, e.g. `{"OFAC": ["RUSSIA-EO14024"], "UK": ["Russia"]}`. UK regimes are shortened
  ("The Russia (Sanctions) (EU Exit) Regulations 2019" becomes "Russia"); UN uses its list type ("Libya");
  EU its programme code ("PRK").
- `listings`: one entry per list with `list`, `id`, `uid`, `name`, `programmes`, `listed_on` and what that
  list adds (`reference`, `updated_on`, `measures`, `designation_source`, `un_ref`, `flag`, `previous_flags`,
  `owner`, `previous_owners`, `length_m`, ...).
- `listed_on`: the earliest listing date any list gives (OFAC's file has none).
- `imos` (vessels): every IMO number the lists give.

`program` stays one OFAC-style string so existing callers that split it on `] [` keep working. The OFAC part
is exactly as published; other lists follow with their name in front: `"RUSSIA-EO14024] [UK: Russia"`, or
`"UK: Russia"` for a UK-only vessel. The ships card reads the lists from these labels until the ships module
passes `lists` through.

New: `version()` changes whenever any list (re)loads, `summary()` gives counts and refresh times per list,
`record(uid)` finds a merged record by any list's uid. `ready()` is true once any list is loaded.

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /api/sanctions/check?imo&mmsi&name&hex&reg&msn` | `{ready, vessel, aircraft, lists}`: `lists` is every list naming either match |
| `GET /api/sanctions/vessels?list=` | `{ready, vessels, lists, counts}`; optional `list` (OFAC, UK, UN or EU) filters |
| `GET /api/sanctions/aircraft?list=` | `{ready, aircraft, lists, counts}` |
| `GET /api/sanctions/lists` | Per list: name, publisher, licence, refresh, vessels, aircraft, vessels not on OFAC, designations, generated date, `built_at`, `last_attempt`, `last_error`, `stale` and `state` (ok, error, building, waiting); plus merged totals |

Search (`/api/core/search`) returns sanctioned vessels from every list by name, alias, IMO or MMSI, and
OFAC aircraft by registration or operator, as kinds `sanctioned_vessel` and `sanctioned_aircraft` with the
lists in the subtitle ("Sanctioned vessel (OFAC, UK), Oil Tanker, IMO 9826902"). The AI tool
`sanctions_lookup` returns the records plus `lists`, `checked` (each loaded list with its publication date)
and a `note` when a vessel matched by name only.

## Failure handling

- Each list builds on its own: one list failing never touches the others.
- Downloads use ETag and If-Modified-Since where the host supports them. When a download fails and an older
  raw copy exists, the core keeps it; the module notices (the file's time did not move) and fails that
  list's build, so its last good build stays in service and on disk, the dataset keeps its last good
  `built_at` and retries on the next round (every 30 minutes while stale). Only when no build exists yet is
  the older raw copy parsed.
- A file that parses badly or looks incomplete (UK under 1,000 designations or 100 ships, UN under 300, EU under
  1,000, OFAC under 100 vessels or 20 aircraft) is refused the same way.
- The list's source turns to an error with the reason ("127.0.0.1: HTTP 503; keeping the UN Security Council
  list built on 23 Nov 2026") and the note "Keeping the copy from 23 Nov 2026."

## Known gaps and ideas

- The ships module copies a fixed set of keys from the match (`ships/store.py`, `_check_sanctions`), so `lists`
  and `programmes` do not reach its cards yet; the card derives the lists from the programme labels. Its
  event subtitle, layer description and `/api/ships/sanctioned` note still say OFAC (backend strings).
- Ships re-matches vessels only when `len(sanctions.vessels())` changes; `sanctions.version()` is the better
  signal.
- Flights cards still say "OFAC-listed", which is right while only OFAC lists aircraft.
- Optional later: OpenSanctions per-source files (`un_1718_vessels`, `eu_journal_sanctions`, CC BY-NC 4.0) for
  the UN 1718 vessel list and the EU shadow-fleet annex; owner and operator matching against designated
  entities; an alert when a vessel designated in the last week is seen in AIS.
