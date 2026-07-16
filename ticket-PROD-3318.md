# Ticket: PROD-3318

- **Reporter:** internal analyst "FinOps team"
- **Priority:** (unset — the agent must classify it)
- **Opened:** 2026-06-11 16:40

## Summary
"The monthly revenue export is coming back empty. I ran the June report this
morning and the CSV downloads fine but has 0 rows. No error message, it just
looks blank. Not urgent — I can pull the numbers manually for now — but would
like it fixed before month-end close."

## Details
- Affected: report export only (checkout, orders, payments all working)
- Symptom: export succeeds (HTTP 200) but returns 0 rows
- Workaround exists: manual query
- No customer impact; single internal user

## Related history
- **PROD-3301** (two weeks ago): report date-range picker changed from
  MM/DD/YYYY to ISO `YYYY-MM-DD`. May be relevant.
