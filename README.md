# MileMap

**The price of a mile, wherever you live.** A free public map of the whole UK showing what it costs to drive one mile by petrol, diesel, EV charged at home and EV charged at a public charger, plus how local fuel markets behave, who pays the most, and the UK's first open archive of every fuel price change.

> Status: **Phase 1, data foundation.** Nothing public yet. See [`docs/phase-1-plan.md`](docs/phase-1-plan.md).

## Run locally

Requires [uv](https://docs.astral.sh/uv/). Python 3.12 is installed automatically.

```bash
cp .env.example .env        # then fill in your own credentials (never commit .env)
uv sync
uv run pytest
uv run milemap probe history-depth --quick   # Fuel Finder needs a UK IP address
```

## Data sources and attribution

Contains public sector information licensed under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/) (Fuel Finder).

## Licence

Code: MIT (see [LICENSE](LICENSE)). Data licences are listed per source.
