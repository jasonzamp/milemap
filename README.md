# MileMap

**The price of a mile, wherever you live.** A free public map of the whole UK showing what it costs to drive one mile by petrol, diesel, EV charged at home and EV charged at a public charger, plus how local fuel markets behave, who pays the most, and the UK's first open archive of every fuel price change.

> Status: **Phase 1, data foundation.** The UK price archive has been recording since
> **4 November 2026, 22:15 UTC** (every 5 minutes). Nothing public yet.
> See [`docs/phase-1-plan.md`](docs/phase-1-plan.md).

## Docs

- [History depth](docs/history-depth.md): what the Fuel Finder API holds (latest state only, so we record history ourselves)
- [Raw storage and the collector](docs/raw-storage.md): layout, write-once guarantees, schedule, gaps
- [Compliance](docs/compliance.md): Fuel Finder rate limits, the Fair Use Policy and display rules

## Run locally

Requires [uv](https://docs.astral.sh/uv/). Python 3.12 is installed automatically.

```bash
cp .env.example .env        # then fill in your own credentials (never commit .env)
uv sync
uv run pytest
uv run milemap probe history-depth --quick   # Fuel Finder needs a UK IP address
uv run milemap collect tick                  # one collector run (writes data/, uploads to R2)
uv run milemap collect gaps                  # coverage and gaps over the last 7 days
sh deploy/mac/install.sh                     # run the collector every 5 min via launchd
```

## Data sources and attribution

Contains public sector information licensed under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/) (Fuel Finder).

## Licence

Code: MIT (see [LICENSE](LICENSE)). Data licences are listed per source.
