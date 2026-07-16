# Widget Reference — Xray Test Repository Cross-Reference

Source: Jira/Xray Cloud project **CXDV** (CXone Data Visualization), Test Repository, synced 2026-07-15 via Xray Cloud GraphQL API (client_id/client_secret auth — see `configuration/local.properties`, `JIRA_AUTH_CREDS`).

This file cross-references the 37 widgets this repo automates (see root `CLAUDE.md` widget catalog) against existing manual/automated test scenarios already defined in the Xray Test Repository, so new Playwright specs can be generated/aligned against real coverage instead of guessed from scratch.

**Scope note:** the CXDV Jira project also contains folders for unrelated sub-teams (ETL, Gamification, Application Analytics, BI Reports MSTR, CXCV IL) — those are NOT part of this repo's scope and are excluded below.

**Caveat:** folder-based Xray search returns tests filed directly under each widget's dedicated folder tree (mostly under `/CXDV_20Mar2026/New widgets- Waves & Technocrats/*` and `/CXDV_20Mar2026/Dashboards/ACD`). Older/duplicate coverage also exists scattered under per-release QA folders (e.g. `/CXDV_20Mar2026/Dragonfly/25.1_QA/QM Widgets/*`) which are not fully enumerated here — treat counts below as a floor, not an exhaustive total.

---

## ACD widgets (11) — Standard license

### Agent State Counter, Agent Contact View, Agent List, Agent State Summary, Contact States by Skill, Contact List, Service Level, Queue Counter, Callback Requests, Call Arrival, Dispositions (combined folder)
- Xray folder: `/CXDV_20Mar2026/Dashboards/ACD`
- Test count: 95
- Sample scenarios:
  - CXDV-58511: [Agent Contact View Widget] - Verify the default column available in the grid of the widget
  - CXDV-37849:  [Agent State Summary] - Verify search functionality and column rearrangement in widget grid
  - CXDV-28643: [Agent State Summary ] [Settings] -'Show Unavailable States' Check box funcationality
  - CXDV-58535: [Agent Contact View Widget] -Verify Display Name in settings options
  - CXDV-58508: [Agent Contact View Widget] - Verify No data avaliable State of the Widget
  - CXDV-42753: CXCV-25070 TC2 -	Toggling fullscreen on AG Grid widgets does not trigger the publish bar - Contact List
  - CXDV-63610: [Agent Contact View  Widget] - E2E --> Review widget with Agent's data
  - CXDV-60024: [Agent List Widget]- Verify the 'Expand All Row Groups' and 'Collapse All Rows Groups' functionality
  - CXDV-43629: CV Dashboards > Agent List > Verify data within the Multi-Channel and Skill View
  - CXDV-58514: [Agent Contact View Widget] - Verify multi sorting
  - CXDV-48850: [Interaction Summary Widget -Settings]- Verify "direction" filter in the settings modal
  - CXDV-20401: [Smoke] Dashboards > Service Level review with DFO data
  - CXDV-11998: [Call Arrival widget]- verify legend position
  - CXDV-28448: [Agent State Summary ] - Verify Duplicate widget functionality
  - CXDV-13351: Dashboards > Contact States by Skill review with ACD data generated for PostQueue state
  - CXDV-13415: [Smoke] Dashboards > Service Level review with ACD data
  - CXDV-13423: [Smoke] Dashboards > Service Level review without data
  - CXDV-20028: Dashboards > Omnichannel - Verify DFO and ACD contacts with dispositions are displayed for Service Level in all Metric widgets
  - CXDV-28958: Dashboards > Contact States by Skill review with Digital data generated for Prequeue state
  - CXDV-79414: Dashboards > Queue Counter review with data for  Longest delay callback

Individual widget folders (post-migration, more granular):
### Call Arrival Widget
- Xray folder: `/CXDV_20Mar2026/New widgets- Waves & Technocrats/Call Arrival Widget`
- Test count: 22
- Sample scenarios:
  - CXDV-11923: [Call arrival- settings]- verify Agent dropdown
  - CXDV-12271: [Call Arrival- Settings]- Verify date range and time field.
  - CXDV-11920: [Call Arrival widget- Settings]- Verify display name
  - CXDV-11990: [Call arrival- settings]- verify save and cancel buttons
  - CXDV-11922: [Call Arrival Widget- Settings]- Verify Team dropdown
  - CXDV-11909: [Call Arrival Widget]- Verify error messages
  - CXDV-12001: [Call arrival widget]-Verify export functionality
  - CXDV-12000: [Call arrival widget]-Verify manual  and automatic refresh

### Contact States by Skill
- Xray folder: `/CXDV_20Mar2026/New widgets- Waves & Technocrats/Contact States by Skill`
- Test count: 17
- Sample scenarios:
  - CXDV-42294: [Contact States by Skill] - OutQueue> Data validation
  - CXDV-39027: [Contact States by Skill] - Verify the default column available in the grid of the widget
  - CXDV-39016: [Contact States by Skill  Widget- Context menu]- Verify context menu
  - CXDV-42292: [Contact States by Skill] - PostQueue> Data validation
  - CXDV-41924: [Contact States by Skill] - InQueue> Data validation
  - CXDV-39025: [Contact States by Skill - Verify search functionality
  - CXDV-39018: [Contact States by Skill] - Verify Duplicate widget functionality
  - CXDV-39017: [Contact States by Skill ] - Verify manual refresh of the widget

### Dispositions widget
- Xray folder: `/CXDV_20Mar2026/New widgets- Waves & Technocrats/Dispositions widget`
- Test count: 30
- Sample scenarios:
  - CXDV-37813: [Dispositions widget- Settings]- Verify Team filter
  - CXDV-25396: [Dispositions widget]- Verify automatic refresh
  - CXDV-37623: [Disposition widget- table view]- Verify number and name of the columns
  - CXDV-25391: [Dispositions widget]- Verify user can select upto last 25 months dates
  - CXDV-25386: [Dispositions widget]- Verify Grid header options
  - CXDV-28447: [Dispositions widget- context menu]- Verify Legend position
  - CXDV-25364: [Dispositions Widget - Settings ]- Verify search functionality of "Teams" , "Agent", "Campaign", "Skill", and "Campaign" on settings modal
  - CXDV-25325: [Dispositions Widget - Context menu]- Verify refresh Functionality from Hamburger menu

### Agent List Widget
- Xray folder: `/CXDV_20Mar2026/New widgets- Waves & Technocrats/Agent List Widget`
- Test count: 23
- Sample scenarios:
  - CXDV-58903: [Agent List Widget]- Verify that the 'Expand All Row Groups' option should be visible and functional.
  - CXDV-60908: Dashboard > Global export validation for dashboards
  - CXDV-56637: [Agent List Widget]- Force logout and Terminate option for any logged-in agent should be available for Supervisor
  - CXDV-60030: [Agent List Widget]- Verify toggle behavior with no qualifying agents.
  - CXDV-60025: [Agent List Widget]- Verify real-time agent state changes with toggle ON.
  - CXDV-61148: [Agent List Widget]- Verify that the 'Expand All Row Groups' option should be visible and functional for 10 agents with more than 10 contacts.
  - CXDV-60023: [Agent List Widget]- Verify toggle only expands target states, ignores others.
  - CXDV-61208: [Agent List Widget]- Verify the state persistence- Navigate in the other areas of the application and return.

### Agent Contact View
- Xray folder: `/CXDV_20Mar2026/New widgets- Waves & Technocrats/Agent Contact View`
- Test count: 10
- Sample scenarios:
  - CXDV-58546: [Agent Contact View Widget]  - verify the state of the widget after resize
  - CXDV-58519: [Agent Contact View Widget]- Verify user can rearrange the position of columns widget grid
  - CXDV-58505: [Agent Contact View Widget]- Verify context menu
  - CXDV-58518: [Agent Contact View Widget] - Verify remove widget option
  - CXDV-58513: [Agent Contact View Widget]- Verify search functionality of "Teams" and "Agent"on settings modal
  - CXDV-58502: [Agent Contact View Widget] -  Verify the header options
  - CXDV-58515: [Agent Contact View Widget] - Verify "Select All" and "Clear All" functionality for teams and agent drop downs on settings modal
  - CXDV-58501: [Agent Contact View Widget]- Verify user is able to add widget into dashboard

### Agent State Summary widget
- Xray folder: `/CXDV_20Mar2026/New widgets- Waves & Technocrats/Agent State Summary widget`
- Test count: 7
- Sample scenarios:
  - CXDV-37969: [Agent State Summary ] - Working Contacts [Outbound] > Data validation
  - CXDV-37965: [Agent State Summary ] - Available > Data validation
  - CXDV-38086: [Agent State Summary ]-Verify old dashboard having Agent State Summary widget after migration to new arch
  - CXDV-37967: [Agent State Summary ] - Unavailable State > Data validation
  - CXDV-38052: [Agent State Summary ] - Dialer > Data validation
  - CXDV-37970: [Agent State Summary ] - Working Contacts [Inbound] > Data validation
  - CXDV-37850: [Agent State Summary] - Verify Info icon of the widget

### Callback Request
- Xray folder: `/CXDV_20Mar2026/New widgets- Waves & Technocrats/Callback Request`
- Test count: 15
- Sample scenarios:
  - CXDV-38256: [Callback Requests- context menu]- Verify Legend position
  - CXDV-38246: [Callback Requests] -  Verify the header options of Callback Requests widget
  - CXDV-38252: [Callback Requests]- Verify automatic refresh
  - CXDV-38327: [Callback Requests] - Callback InQueue> Data validation
  - CXDV-38451: [Callback Requests] - Callback Success> Data validation
  - CXDV-38253: [Callback Requests]-Verify settings options
  - CXDV-38235: [Callback Request ]- Verify user is able to add widget into dashboard
  - CXDV-38463: [Callback Requests ]-Verify old dashboard having Callback Requests widget after migration to new arch

### Interaction Summary Widget
- Xray folder: `/CXDV_20Mar2026/New widgets- Waves & Technocrats/Interaction Summary Widget`
- Test count: 31
- Sample scenarios:
  - CXDV-47030: [Interaction Summary Widget]- Verify user is able to add a widget into the dashboard.
  - CXDV-47715: [Interaction summary Widget] - Verify contact details report is open when click on interaction id
  - CXDV-47036: [Interaction Summary Widget - Empty State ]- Verify No Data available
  - CXDV-48523: [Interaction summary widget]- Verify widget context menu options
  - CXDV-47445: [Interaction Summary Widget -Settings]- Verify "Team" filter in the settings modal
  - CXDV-47037: [Interaction Summary Widget - Context Menu] - Verify user is able to duplicate the widget
  - CXDV-47534: [Interaction Summary Widget]- Verify skill tooltip on widget display
  - CXDV-47309: [Interaction Summary Widget]- Verify user can select upto previous 25 months date

---

## QM (Quality Management) widgets (10) — QM License

### Quality Evaluations widget
- Xray folder: `/CXDV_20Mar2026/New widgets- Waves & Technocrats/Quality Evaluations widget`
- Test count: 64
- Sample scenarios:
  - CXDV-30533: [Quality Evaluations widget]- Verify user is able to export dashboard having Quality evaluation widget
  - CXDV-56156: [Quality Evaluations widget- settings]- Verify threshold bar UI resizes properly on different screen sizes.
  - CXDV-56142: [Quality Evaluations widget- settings]- Verify default threshold values are 0 (min) and 100 (max).
  - CXDV-29324: [Quality Evaluations widget]- Verify play button for each evaluations details
  - CXDV-32408: [Quality evaluations widget]- Verify context menu
  - CXDV-56152: [Quality Evaluations widget- settings]- Verify toggle between views rapidly without causing UI issues
  - CXDV-31712: [Quality Evaluations widget- settings]- Verify groups dropdown
  - CXDV-21461: [Quality evaluations widget]- Verify user can rearrange the position of columns widget grid

### Quality Score Widget
- Xray folder: `/CXDV_20Mar2026/New widgets- Waves & Technocrats/Quality Score Widget`
- Test count: 47
- Sample scenarios:
  - CXDV-27772: [Quality Score Widget]- Verify selected date range tooltip on widget display for all the views
  - CXDV-27611: [Quality Score widget- context menu]- Verify grid columns when widget is in evaluations or channel view
  - CXDV-27616: [Quality Score widget-Table views] - Verify search functionality
  - CXDV-36003: [Quality score widget- All views]- Verify Date paradigm dropdown on the settings modal
  - CXDV-29770: [Quality Score Widget - scoreboard view - Data validation]- verify average team score
  - CXDV-29013: [Quality Score widget- channel view]- Verify export functionality for channel view
  - CXDV-27604: [Quality Score Widget- Table views]- Verify columns option
  - CXDV-36006: [Quality score widget- All views]- Verify Form name & form version on the settings modal

### Evaluator Calibration Widget (migration)
- Xray folder: `/CXDV_20Mar2026/New widgets- Waves & Technocrats/Evaluator Calibration Widget- Migration`
- Test count: 20
- Sample scenarios:
  - CXDV-30774: [Evaluator calibration widget] - Verify tooltip for evaluator name on widget display
  - CXDV-30777: [Evaluator calibration widget] - Verify default 6 of evaluators with highest variance on chart
  - CXDV-30744: [Evaluator Calibration widget] - Verify manual refresh from widget context menu
  - CXDV-30773: [Evaluator calibration widget] - Verify my zone link
  - CXDV-30611: [Evaluator calibration widget]- verify date range tooltip on widget display
  - CXDV-30775: [Evaluator Calibration widget] - Verify x-axis and y-axis labels
  - CXDV-30771: [Evaluator calibration widget]- Verify remove widget functionality
  - CXDV-31006: [Evaluator calibration widget] - Verify export dashboard with evaluator calibration widget

### Forms Calibration widget (migration)
- Xray folder: `/CXDV_20Mar2026/New widgets- Waves & Technocrats/Forms Calibration widget - Migration`
- Test count: 19
- Sample scenarios:
  - CXDV-31045: [Forms calibration widget] - E2E --> Validate forms variance data
  - CXDV-30901: [Forms Calibration widget] - Verify export dashboard with forms calibration widget
  - CXDV-37152: [Form Calibration]- Verify Form name & form version on the settings modal
  - CXDV-30869: [Forms calibration widget] - Verify form calibration variance in bar chart
  - CXDV-30875: [Forms calibration widget] - Verify default 6 forms with highest variance on chart
  - CXDV-30859: [Forms calibration] - Verify auto refresh functionality
  - CXDV-30853: [Forms calibration widget]- Verify settings options
  - CXDV-30868: [Forms Calibration widget] - Verify x-axis and y-axis labels

Older per-release QM widget coverage (Dragonfly 25.1_QA — not deep-fetched, counts only): Evaluator Performance Widget (32), Evaluation and Coaching Trend widget (22), Plan Status (22), Coaching Status Widget (18), Quality Score Widget (30), Quality Evaluation Widget (30) — see `/CXDV_20Mar2026/Dragonfly/25.1_QA/QM Widgets/*`.

---

## Coaching widgets (2) — Coaching License

### Coaching Status Widget
- Xray folder: `/CXDV_20Mar2026/New widgets- Waves & Technocrats/Coaching Status Widget`
- Test count: 25
- Sample scenarios:
  - CXDV-35593: [Coaching Status] - E2E data validation
  - CXDV-33094: [Coaching Status] - Verify  the status w.r.t "Assigned to me" Tab
  - CXDV-7299: [Coaching Status] -Verify coaching status widget export functionality.
  - CXDV-33092: [Coaching Status] - Verify Auto refresh of the widget
  - CXDV-7004: [CXCV-6547]Verify Permission for Coaching status widget
  - CXDV-7011: [Coaching Status] -Verify the Basic skeleton of Coaching Status widget..
  - CXDV-7212: [Coaching Status] - Verify the "Coaching type" filter in coaching Status Widget
  - CXDV-7159: [Coaching Status] - Verify the Pie-chart for "Owned by me " and "Assigned to me" Tabs with legends and filters.

### Evaluations and Coaching Trends (KPI Trend and Coaching Events)
- Xray folder: `/CXDV_20Mar2026/New widgets- Waves & Technocrats/Evaluations and Coaching Trends`
- Test count: 21
- Sample scenarios:
  - CXDV-16114: [Evaluation and Coaching Trend] - Verify the Date range dropdown of  evaluations and coaching trends
  - CXDV-16187: [Evaluation and Coaching Trend] - Verify the Evaluation and Coaching marks
  - CXDV-16029: [Evaluation and Coaching Trend] - Verify Duplicate widget functionality
  - CXDV-16124: [Evaluation and Coaching Trend] - Verify export functionality
  - CXDV-16019: [Evaluation and Coaching Trend] - Verify the header options of Evaluation and Coaching Trend widget
  - CXDV-16024: [Evaluation and Coaching Trend] - [Settings] - Verify Team dropdown
  - CXDV-16189: [Evaluation and Coaching Trend] - Verify the Tooltip of Evaluation
  - CXDV-16032: [Evaluation and Coaching Trend] - Verify remove functionality of the widget

---

## Metrics widgets (9) — Standard (PM license for objectives)

### Metrics (general — KPI, Metric Breakdown, Metrics Summary, Metrics Interval, Metrics Review, Leaderboard)
- Xray folder: `/CXDV_20Mar2026/New widgets- Waves & Technocrats/Metrics`
- Test count: 27
- Sample scenarios:
  - CXDV-56282: [Metric Breakdown - Widget] Time format dropdown should include 'HH:mm:ss.SSS' option
  - CXDV-56280: [Metrics Interval Widget] Time format dropdown should include 'HH:mm:ss.SSS' option
  - CXDV-58486: [KPI Trend Widget] Verify Export Functionality When 'Export as PNG' is Selected
  - CXDV-67607: Test to validate the Neutral values should be shown as "blank" in "My Agent" widget.
  - CXDV-58483: [Metrics Review Widget] Verify Export Functionality When 'Export as Excel or CSV' is Selected
  - CXDV-58482: [Metrics Interval Widget] Verify Export Functionality When 'Export as PNG' is Selected
  - CXDV-56288: [KPI Widget] Time format dropdown should include 'HH:mm:ss.SSS' option
  - CXDV-56277: [Metrics Review Widget] Time format dropdown should include 'HH:mm:ss.SSS' option

### KPI Trends
- Xray folder: `/CXDV_20Mar2026/New widgets- Waves & Technocrats/KPI Trends`
- Test count: 59
- Sample scenarios:
  - CXDV-28262: [KPI Trends- view by dropdown]- Verify data when user selects a team/agent/ skill/ campaign in view by dropdown
  - CXDV-7637: [KPI trend widget -Settings]- Verify select metric modal
  - CXDV-26069: [KPI Trends widget- View By]- Verify team/agent/skill/campaign dropdown is not display over the widget when Company option is selected In the view by dropdown
  - CXDV-7677: [KPI Trend Widget-Graph] - Verify X and Y axis values on the graph
  - CXDV-7224: [KPI Trend Widget]- Verify Prev <Data Duration> label
  - CXDV-7609: [KPI Trend Widget - Settings ]- Verify search functionality of "Teams" , "Agent", "Campaign", "Skill", and "Media Type" on settings modal
  - CXDV-26156: [KPI trends widget - view by ]- Verify synchronization between widget display dropdown and settings dropdown
  - CXDV-21121: [KPI trends Objectives]- Verify the tooltip on the graph when an objectives is visible

### Gauge widget
- Xray folder: `/CXDV_20Mar2026/New widgets- Waves & Technocrats/Gauge widget`
- Test count: 19
- Sample scenarios:
  - CXDV-8481: [Gauge widget] - Verify the widget if there is no objectives is defined
  - CXDV-8359: [Gauge widget] - Verify the Refresh Icon and its functionality of Gauge Widget
  - CXDV-8400: [Gauge widget] - Verify "Settings" Options of Gauge Widget.
  - CXDV-8367: [Gauge widget] - Verify the Basic skeleton of  Gauge Widget.
  - CXDV-8372: [Gauge widget - Settings ]- Verify search functionality of "Teams" , "Agent", "Campaign", "Skill",  on settings modal
  - CXDV-8363: [Gauge widget] - Verify the "Duplicate" Functionality in Gauge Widget
  - CXDV-8429: [Gauge widget -  Empty State] - Verify error messages of Gauge widget.
  - CXDV-8760: Verify the metric distribution for normal and hover mode of Gauge widget

### Widget Metrics (data-validation focused, cross-widget)
- Xray folder: `/CXDV_20Mar2026/Guardians (Only for Reference)/ClearView widgets/Widget Metrics`
- Test count: 184 (showing first 100)
- Sample scenarios:
  - CXDV-37039: [NRT] DFO > Generate a Chat with assignation and Escalated state
  - CXDV-35026: [NRT] Agent > Generate Agent Preview State
  - CXDV-35008: [NRT] ACD > Generate Prequeued Chat
  - CXDV-35020: [NRT] ACD > Generate Email Forward
  - CXDV-19887: Digital > Configuration and generation > Live Chat Contacts with Disposition
  - CXDV-13529: ACD > Historical > % Queued
  - CXDV-35017: [NRT] ACD > Generate Interrupted Chat
  - CXDV-35014: [NRT] ACD > Generate Scheduled Callback
  - CXDV-28603: Digital > Generate contact with CXone Line channel
  - CXDV-28608: Digital > Generate contact with CXone Whatsapp channel
  - CXDV-53783: Digital > Generate Digital Contacts with ACW
  - CXDV-20191: Service Level > Generate DFO Contact with all states without skill ID
  - CXDV-28580: Digital > Generate contact with CXone Telegram channel
  - CXDV-46352: [NRT] Agent Contact > Generate DFO without focus contact
  - CXDV-46351: [NRT] Agent Contact > Generate Basic DFO Interaction

---

## PM (Performance Management) widgets (4) — PM License

### Out of Adherence Cause widget (OOA)
- Xray folder: `/CXDV_20Mar2026/New widgets- Waves & Technocrats/OOA Cause widget`
- Test count: 9
- Sample scenarios:
  - CXDV-56505: [OOA cause widget] Tooltip should be visible on hover with defined data and design
  - CXDV-56513: [OOA cause widget] Validate Chart Filtering Based on Selection and Deselection of Multiple Drop down Values
  - CXDV-56503: [OOA cause] Verify correct activity count with labels visible on each child tile
  - CXDV-56531: Settings have additional Filter Scheduled activity and Unit Scheduled
  - CXDV-57679: [OOA Cause Widget] - Verify widget when no activity is selected
  - CXDV-56506: [OOA cause] Verify Dropdown list is binded with API Data
  - CXDV-55714: [OOA Cause Widget - E2E --> Data validation
  - CXDV-56528: Verify Context menu should have "Show Legend"

### Agent Adherence Variance
- Xray folder: `/CXDV_20Mar2026/New widgets- Waves & Technocrats/Agent Adherence Variance`
- Test count: 1
- Sample scenarios:
  - CXDV-58568: Dashboard > Global export validation for dashboards

Evaluator Performance Widget / Plan Status coverage lives under the QM folders above (shared PM+QM dependency).

---

## IA (Interaction Analytics) widget (1) — IA License

Frustration widget: no dedicated Xray folder found under the CXCV Dashboard tree in this sync — search project-wide with `jql: "project = CXDV AND summary ~ Frustration"` if needed later.

---

## Cross-cutting functional coverage (applies across all widgets)

### Functional
- Xray folder: `/CXDV_20Mar2026/Guardians (Only for Reference)/ClearView widgets/Functional`
- Test count: 168 (showing first 100)
- Sample scenarios:
  - CXDV-32448: Dashboards > KPI Trend > Verify Horizontal objective color bar is displaying properly when only one range is displayed
  - CXDV-21876: Dashboard > Verify that after enable agent zoom permission, agent state details is displayed and can be opened
  - CXDV-55227: Dashboard > Report > Verify that "Agent Contact Detail" doesn't have negative numbers and not congruent datetime value
  - CXDV-34498: Dashboard > ACD > Report > Dashboard that contains Report widget can be exported
  - CXDV-56067: Dashboard > ACD > Verify that [None] value is displayed in ACD widgets
  - CXDV-21734: Impersonation > Dashboard > Metrics breakdown > Verify is widget is displayed in the dashboard
  - CXDV-20181: Dashboard > Service Level > Review data when have DFO contact is reassigned to another queue
  - CXDV-47105: Dashboards > Service Level > Verify Direction filter functionality after migration
  - CXDV-30288: Dashboard > ACD > Report > Widget should display data when change report to Contact Detail mode
  - CXDV-21873: Dashboards > Check if the viewer for a dashboard he doesn't own can update column options (resize, sort and filter)
  - CXDV-20608: CV Dashboard > Agent List > Table mode > Verify that widget is updated after check and uncheck visualization options
  - CXDV-30475: Dashboards > Agent State Counter > Verify Hide legends in the widget's options.
  - CXDV-41979: [AppLink] Dashboards > Metrics > Exploratory Testing
  - CXDV-34057: Dashboards > Report widget > When exporting CSV files with specific languages containing special characters, the text is NOT converted to symbols.
  - CXDV-21875: Dashboard > Verify that after disable agent zoom permission, agent state details is hidden

### RBAC Data Restriction
- Xray folder: `/CXDV_20Mar2026/Guardians (Only for Reference)/ClearView widgets/RBAC Data Restriction`
- Test count: 58
- Sample scenarios:
  - CXDV-36668: Dashboard > RBAC > When review a shared dashboard with a user without views, data in all Metrics is displayed
  - CXDV-36653: Dashboard  > RBAC > Teams > When review a shared dashboard with a user with views, data in Coaching widgets are matching according to views
  - CXDV-36626: Impersonation > Dashboard > RBAC > Verify when user has no assigned views, they can access all data in PM widgets
  - CXDV-36655: Dashboard  > RBAC > Skills > When review a shared dashboard with a user with views, data in ACD widgets are matching according to views
  - CXDV-36648: Dashboard  > RBAC > Teams > When review a shared dashboard with a user with views, data in ACD widgets are matching according to views
  - CXDV-25890: Dashboard > RBAC > Skills > Verify that ACD widget settings are limited according to view
  - CXDV-25920: PM > Dashboard > RBAC > Teams > Verify that user with a view assigned only see data of selected teams
  - CXDV-36651: Dashboard  > RBAC > Teams > When review a shared dashboard with a user with views, data in QM widgets are matching according to views
  - CXDV-36618: Dashboard > RBAC > Skills > Verify that user with assigned view only can see data of selected skills in Coaching widgets
  - CXDV-36647: Dashboard  > RBAC > Skills > Verify that users without views can see all data in PM widgets

### Localization
- Xray folder: `/CXDV_20Mar2026/Guardians (Only for Reference)/ClearView widgets/Localization`
- Test count: 28
- Sample scenarios:
  - CXDV-47910: [Localization - Hebrew] Dashboard > QM > Evaluation and Coaching Trend > Verify that widget is correctly located
  - CXDV-47905: [Localization - Hebrew] Dashboard > ACD > Call Arrival > Verify that widget is correctly located
  - CXDV-47929: [Localization - Hebrew] Dashboard > Performance Management > Challenges > Verify that widget is correctly located
  - CXDV-47926: [Localization - Hebrew] Dashboard > Metrics > Metrics Interval > Verify that widget is correctly located
  - CXDV-47909: [Localization - Hebrew] Dashboard > QM > Category Over Time > Verify that widget is correctly located
  - CXDV-47914: [Localization - Hebrew] Dashboard > QM > Forms Calibration > Verify that widget is correctly located
  - CXDV-47915: [Localization - Hebrew] Dashboard > QM > Plan status > Verify that widget is correctly located
  - CXDV-47911: [Localization - Hebrew] Dashboard > QM > Evaluation and Coaching Events > Verify that widget is correctly located
  - CXDV-47900: [Localization - Hebrew] Dashboard > ACD > Contact States By Skill > Verify that widget is correctly located
  - CXDV-47906: [Localization - Hebrew] Dashboard > ACD > Dispositions > Verify that widget is correctly located

### Permissions
- Xray folder: `/CXDV_20Mar2026/Guardians (Only for Reference)/ClearView widgets/Permissions`
- Test count: 8
- Sample scenarios:
  - CXDV-21685: Dashboard > After disabling PM permissions at role level verify if user can create dashboards and add widgets
  - CXDV-21687: Impersonation > Dashboard > After disabling PM app in the tenant verify if user can create dashboards and add widgets
  - CXDV-30207: [Negative Testing] Dashboards > Templates > Verify that Template option and permissions are not displayed
  - CXDV-21688: Dashboard > After disabling PM app in the tenant, verify if user can create dashboards and add widgets
  - CXDV-21686: Impersonation > Dashboard > After disabling PM permissions at role level verify if user can create dashboards and add widgets
  - CXDV-21516: Performance Management > Dashboard > Verify if user can access to CV Dashboards
  - CXDV-21514: Dashboard > Verify if user can access to CV Dashboards
  - CXDV-21515: [Impersonation] Dashboard > Verify if user can access to CV Dashboards

### Timezone
- Xray folder: `/CXDV_20Mar2026/Guardians (Only for Reference)/ClearView widgets/Timezone`
- Test count: 1
- Sample scenarios:
  - CXDV-24383: Dashboard > Verify if Dashboard is listing 3 or more time zones in Dashboard Timezone list

### Navigation
- Xray folder: `/CXDV_20Mar2026/Guardians (Only for Reference)/ClearView widgets/Navigation`
- Test count: 6
- Sample scenarios:
  - CXDV-19532: [Exploratory] Dashboard > QM > Verify widgets functionality in different browsers
  - CXDV-19531: [Exploratory] Dashboard > Analytics > Verify widgets functionality in different browsers
  - CXDV-19529: [Exploratory] Dashboard > Coaching > Verify widgets functionality in different browsers
  - CXDV-19528: [Exploratory] Dashboards > Performance Management > Verify widgets functionality in different browsers
  - CXDV-19527: [Exploratory] Dashboards > Verify Manage Dashboards functionality in different browsers
  - CXDV-19521: [Exploratory] Dashboards > Verify that navigation and projection is working

### Smoke
- Xray folder: `/CXDV_20Mar2026/Guardians (Only for Reference)/ClearView widgets/Smoke`
- Test count: 12
- Sample scenarios:
  - CXDV-13435: [Smoke][Real Time] Dashboards > Agent List review with agent's data
  - CXDV-13406: [Smoke][Real Time] Dashboards > Agent List review with contact's data
  - CXDV-16303: Dashboards > Metrics review when having a metric with handled historical data selected
  - CXDV-46975: [25.2][Smoke] Dashboards > Agent List > Agent state detail review with agent's data
  - CXDV-13416: [Smoke] Dashboards > KPI Trend review when having a metric with data selected
  - CXDV-13425: [Smoke] [Real Time] Dashboards > Agent State Counter review with agent data generated
  - CXDV-13421: [Smoke] Dashboards > Contact List review with contact data generated
  - CXDV-13419: [Smoke][Real Time] Dashboards > Agent State Counter review with Preview and Waiting data generated [Personal Connection]

### Edit Mode
- Xray folder: `/CXDV_20Mar2026/Guardians (Only for Reference)/ClearView widgets/Edit Mode`
- Test count: 15
- Sample scenarios:
  - CXDV-26229: Dashboard > Metrics > When add Metrics widgets, select metrics and wait for refresh, publish bar should not to be displayed
  - CXDV-26238: Dashboard > Coaching/ Analytics > After adding any widget with a grid displayed, publish bar should not be displayed after save dashboard
  - CXDV-26224: Dashboard > When navigate to any dashboard, publish bar is not displayed in all navigated dashboards
  - CXDV-26217: Dashboard > Agent List, Contact List, Contact States By Skill > Verify that after generating a contact, empty widgets do not activate the publish bar when they are refreshed and have contact displayed
  - CXDV-26240: Dashboard > Leaderboard> After add Leaderboard widget with a displayed grid and saving before the widget is fully loaded, dashboard should not display publish bar when access to the dashboard or refresh the page
  - CXDV-26247: Dashboard > QM > After adding widgets and wait for automatic refresh, publish bar should not to be displayed
  - CXDV-26233: Dashboard > ACD > When expand and reduced all widgets, publish bar should not to be displayed
  - CXDV-56843: Dashboard > Metrics Summary > After add Metrics Summary widget with a displayed grid and saving before the widget is fully loaded, dashboard should not display publish bar when access to the dashboard or refresh the page

### Date Picker
- Xray folder: `/CXDV_20Mar2026/Guardians (Only for Reference)/ClearView widgets/Date Picker`
- Test count: 27
- Sample scenarios:
  - CXDV-24972: [Exploratory] Dashboard > Metrics > Verify widgets functionality in different browsers
  - CXDV-24956: Dashboard > Metrics > KPI Trend > Verify that after selecting Yesterday, data is displayed for the selected date
  - CXDV-24960: Dashboard > Metrics > Metric Breakdown > Verify that after selecting Yesterday, data is displayed for the selected date
  - CXDV-24957: Dashboard > Metrics > KPI Trend > Verify that after selecting Last Week, data is displayed for the selected date
  - CXDV-24959: Dashboard > Metrics > Leaderboard > Verify that after selecting Last Month, data is displayed for the selected date
  - CXDV-24951: Dashboard > Metrics > Gauge > Verify that after selecting Yesterday, data is displayed for the selected date
  - CXDV-24963: Dashboard > Metrics > Metrics Interval > Verify that after selecting Last 30 Days, data is displayed for the selected date
  - CXDV-24943: Dashboard > QM > Category Over Time > Verify if date picker is correctly aligned, contains all expected options and have last 7 days selected by default

Note: this folder is labeled "(Only for Reference)" in Xray — treat as historical/legacy context, not a live source of truth for current widget behavior.
