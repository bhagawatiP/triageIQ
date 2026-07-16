# CXone Dashboard & Reporting Knowledge Base
## Learnings Derived from Existing Bugs, Validations, and Investigations

This knowledge base captures actual product behavior, known limitations, expected functionality, and repeated defect patterns discovered through bugs analyzed so far.

# 1. Agent Visibility Rules
- View By = Agent → Inactive agents are NOT displayed.
- View By = Team → Inactive agents are included.
- Inactive agents are not available in the Agent dropdown.

# 2. Dashboard Widget Initialization Problems
- Some widgets initialize incorrectly.
- Opening Settings and closing them may make data appear.
- Seen in KPI Trend, Days of Week, and Suite Metrics.

# 3. Hard Refresh Dependencies
- Ctrl+Shift+R may resolve Suite Metrics and metadata-loading issues.
- Often indicates cache or state initialization problems.

# 4. API vs UI Mismatch Pattern
- API contains data but UI is empty.
- Common in Plan Status and Hierarchy Selection.
- Usually indicates UI rendering or mapping issues.

# 5. Export Problems
- Export click does nothing.
- Export file generated but empty.
- Export values differ from widget values.

# 6. Calibration Behavior
Expected:
Create Calibration → Evaluation Task → Agent Receives Task → Completion → Calibration Widgets Update.

Common failures:
- Calibration task not created.
- Calibration widgets show no data.
- Completed evaluations remain Pending.

# 7. Team Structure Rules
- For some calibration reports TS is not implemented.
- Expected behavior: all forms visible.

# 8. Real-Time Widget Consistency
Queue Counter, Contact List, and Contact States by Skill should be reasonably aligned.

# 9. Call Arrival Widget
Expected: No Data + refresh timer.
Bug pattern: stuck on 'Refreshing now'.

# 10. View By Logic
If popup says 'View By defaults to Agent', Widget Settings should reflect Agent.

# 11. Search Behavior Standards
Search should filter while typing.
Requiring Enter is considered defective behavior.

# 12. Contact Type Rendering
Expected friendly names such as Original, Transferred, Callback.
Raw enums indicate mapping issues.

# 13. Transcript Limits
Observed truncation around 3000 words.
Expected: full transcript available.

# 14. WFM Failure Indicators
General Error OK + SQL exception usually indicates backend/service failure.

# 15. Metrics Summary Widget Patterns
- View By persistence issues.
- Agent dropdown search issues.
- Team/Agent selection persistence issues.
- Reset Columns may not work.

# 16. QM Metrics Risk Area
Metrics such as Raw Score and Total Evaluations Completed have caused:
- KPI failures
- Internal Server Errors
- Infinite refresh loops
- Disabled Save button

# 17. Plan Status Widget Learnings
- Chart and grid may become inconsistent.
- Breakdown Status may show partial plan lists.
- Export may not match widget.

# 18. Hierarchy Selection Learnings
If API returns hierarchy but UI is empty, likely UI binding/rendering issue.

# 19. Agent State Summary
Show Only Unavailable States should hide Available, Working Contacts, Logged In, and Logged Out.

# 20. Dashboard Report Creation
Create My Report works without added widgets; reports may endlessly load after widgets are added.

# 21. Days of Week Control
Selecting all days should satisfy validation.
Known bug: 'Select at least 1 day' still appears.

# 22. Common Root Cause Heuristics
- API has data, UI empty → Frontend rendering.
- Chart correct, grid wrong → Mapping issue.
- Export empty, widget populated → Export layer.
- Fixed after hard refresh → Cache/state issue.
- Infinite refresh → Failed API retry loop.
- Previous dropdown value persists → Persistence issue.
- Internal enum displayed → Translation issue.
- Different widgets show different counts → Aggregation mismatch.
- SQL exception → Backend/service issue.
- Evaluation completed, widgets empty → Data synchronization issue.

# Recommended Triage Mindset
1. Verify expected behavior.
2. Check Agent vs Team logic.
3. Compare API and UI.
4. Validate exports.
5. Test hard refresh.
6. Compare related widgets.
7. Check configuration and permissions.
8. Confirm reproducibility.
