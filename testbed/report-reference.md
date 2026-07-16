# Report Reference — Xray Test Repository Cross-Reference

Source: Jira/Xray Cloud project **CXDV** (CXone Data Visualization), Test Repository, synced 2026-07-15 via Xray Cloud GraphQL API.

Covers the Reports side of the product (34 prebuilt reports, `dv-report-template` suites rt:suite01–09, `dv-dashboard-widgets/historical/suite03` Reports+WFM) and the reporting-adjacent cross-cutting functional suites.

---

## Prebuilt report query coverage (Dragonfly team, per-report Xray folders)

### Report Queries (all prebuilt reports — combined folder)
- Xray folder: `/CXDV_20Mar2026/Dragonfly/Report Queries`
- Test count: 388 (showing first 100)
- Sample scenarios:
  - CXDV-22879: SQL > Campaign Summary > Verify that Contacts Summary - Campaign Level  total values matches with Contact by Campaign for specific campaign and date and time
  - CXDV-25669: SQL > Contact History > Contact Details > Verify data when setting a specific contact No filter.
  - CXDV-25661: SQL > Contact History > Contact List > Verify data when setting Agent Full Name filter
  - CXDV-22896: SQL > List of Teams > Current Team Details - Verify the data when the Team is updated by Impersonation user
  - CXDV-22872: SQL > Campaign Summary > Contacts Detail > Verify data displayed when applying Direction name filter
  - CXDV-22902: SQL > List of Teams > Historical Team Details - Verify the data when filtering by Team Status
  - CXDV-22869: SQL > Campaign Summary > Contacts Detail > Verify data displayed when applying Campaign name filter
  - CXDV-25598: SQL > Contact History > Contact List > Verify data when having ACD / DFO contacts Rejected
  - CXDV-25603: SQL > Contact History > Contact List > Verify data generated for Personal Connection contacts
  - CXDV-22862: SQL > Campaign Summary > Contacts Summary > Skill Level - Verify data displayed when applying Campaign name filter
  - CXDV-25596: SQL > Contact History > Contact List > Verify data when having Conference/Consult data generated
  - CXDV-22976: SQL > Campaign Summary > Contacts Over Time > Verify data displayed in the query when there is a Digital contact that does not have a skill assigned
  - CXDV-22859: SQL > Campaign Summary > Contacts Summary > Skill Level - Verify data generated for ACD and DFO contacts for specific date
  - CXDV-25686: SQL > Contact History > Agent Contact Tags > Verify data when having Tags for ACD/ DFO contacts
  - CXDV-25678: SQL > Contact History > Contact Lifetime > Verify data when having Transfer contacts
  - CXDV-23053: SQL > List of Unavailable Codes > Current Unavailable Codes Details - Verify the data when filtering by unavailable code ACW?
  - CXDV-25667: SQL > Contact History > Contact List > Verify data when Agent changes team assigned
  - CXDV-25602: SQL > Contact History > Contact List > Verify data when having ACD / DFO Abandons contacts generated
  - CXDV-22980: SQL > Campaign Summary > Contacts Summary > Skill Level - Verify data displayed in the query when there is a Digital contact that does not have a skill assigned
  - CXDV-22890: SQL > List of Teams > Current Team Details - Verify the data for the current teams that belongs to the tenant

---

## ACD Reports (per-release QA snapshot, Dragonfly 25.3_QA)

### ACD Reports
- Xray folder: `/CXDV_20Mar2026/Dragonfly/25.3_QA/ACD Reports`
- Test count: 114 (showing first 100)
- Sample scenarios:
  - CXDV-73573: [QA] Functional Validation- List of Agents Report- Verify Division Billing Code as filter and column present in Current List of Agents and Historical List of Agents
  - CXDV-52344: Evaluation Details Report > Create New Evaluations for Tickets and Verify Average Quality Score in Quality Score Widget and Metric Widgets
  - CXDV-48134: Agent Skill assignment>Verify that widgets can be duplicate, remove and hide columns using widget options
  - CXDV-47762: [Smoke] Evaluation Details Report > Verify that all columns are displayed for Evaluation Details widget
  - CXDV-46939: Agent session report > Verify that rows can be filtered with Agent filters
  - CXDV-47049: Interaction Summary Report > Verify that after change dashboard time zone, data in widgets is updated according to selected time zone
  - CXDV-47314: Functional Validation > Evaluations By Team > Verify Evaluations By Team Template Setting Menu
  - CXDV-47548: Functional Validation > Evaluations By Team >  Verify that widgets can be exported in CSV and Excel format
  - CXDV-46982: Agent session report > Verify that rows can be maximized/minimized and can be filtered with Search option
  - CXDV-46937: Agent session report  > Verify that all columns are displayed for Agent session report widgets
  - CXDV-47560: E2E > Evaluator Analysis > Verify 'Evaluator Analysis' updates correctly when new evaluations and QPs are created
  - CXDV-46983: Agent session report > Verify that columns can be moved and sorted
  - CXDV-47319: [Smoke][Permission] Evaluations By Team > Review that template is visible when have permission enable
  - CXDV-48129: Agent Skill Assignment>Verify that rows can be filtered with Contact Group filters
  - CXDV-46978: Agent session report > Verify that widgets can be duplicate, remove and hide columns using widget options
  - CXDV-47780: Evaluation Details Report > Verify that widgets can hide, filter and pin columns
  - CXDV-47035: Interaction Summary Report > Verify Interaction Summary Widget Settings Additional Filters
  - CXDV-48136: Agent Skill assignment>Verify Chart display option
  - CXDV-48132: Agent Skill assignment> Verify that widgets can be exported in CSV and Excel format
  - CXDV-47981: List of Agents> Verify that rows can be maximized/minimized and can be filtered with Search option

---

## QM Reports (per-release QA snapshot, Dragonfly 25.4_QA)

### QM Reports
- Xray folder: `/CXDV_20Mar2026/Dragonfly/25.4_QA/QM Reports`
- Test count: 125 (showing first 100)
- Sample scenarios:
  - CXDV-58920: Functional Validation >Evaluation Details Report > Verify that widgets can be exported in CSV and Excel format
  - CXDV-58931: Evaluation Details Report >Verify that widget can be maximized/minimized and can be filtered with Search option
  - CXDV-57345: [QA] [Localization] ABI > Verify That Dashboard Template Columns Are Localized
  - CXDV-57338: [QA] Functional > ABI > Verify Dashboard Export Functionality
  - CXDV-58926: Evaluation Details Report > Verify data when global filters are applied
  - CXDV-57614: Permission Validation>Evaluation by Section and Question report>Review that Report is visible when the permission is enabled
  - CXDV-57400: [QA] Calibration Report Functional Validation > Evaluator Details View > Columns display, filter and sort
  - CXDV-59082: [Functional][dataValidation]Coaching Transactional Report >Coaching Session Type Summary widget>Verify widgets loads data with correct metrics>Validate View By, % and count are accurate
  - CXDV-58927: Evaluation Details Report > Verify that columns can be moved and sorted
  - CXDV-58294: Functional Validation>Evaluation by Section and Question report>Agent Question Details Widget>Validate the internal widget used as Quality Evaluation Widget with the report set
  - CXDV-57337: [QA] E2E > ABI > Verify Behavior Score widget for Agents
  - CXDV-57360: Functional Validation>Evaluation by Section and Question report>Average Quality Score by Team Widget>Verify Average Score Calculation
  - CXDV-58288: Functional Validation>Evaluation by Section and Question report>Agent Question Details Widget>Validate default date range (7 days)
  - CXDV-58105: Localization>Evaluation by Section and Question Report (QM)>Verify All Widgets and functionality
  - CXDV-58665: [Smoke] Evaluation Details Report> Verify that all columns are displayed and can be moved and sorted for Evaluation Details Report
  - CXDV-59086: [Functional]Coaching Transactional Report >Coaching Session Type (Doughnut Chart)Summary widgets >Filter validation
  - CXDV-59071: [AllWidget] Coaching Transactional Report >All Widgets> Verify that widgets can be minimized ,maximized,duplicated and exported ,shared and auto refresh the data
  - CXDV-58965: [QA] Data Validation > Calibration Report  >Verify 'Calibration Report' data when evaluator or form is deleted
  - CXDV-58664: Functional Validation >Evaluation Details Report>Verify that dashboard can be created with Evaluation Details Report template and display data
  - CXDV-57330: [QA] Functional > ABI > Verify Interaction Behavioural Summary Widget Threshold Settings When Sentiment View Is ON

---

## Custom Reporting

### Custom Reporting (Filters, Categories & Headers)
- Xray folder: `/CXDV_20Mar2026/Custom Reporting`
- Test count: 33
- Sample scenarios:
  - CXDV-79758: [QA][Custom Reporting] Add Filters - Select All in value dropdown selects all values; Clear button removes all selections
  - CXDV-79741: [QA][Custom Reporting] Add Filters - Metric Group operator dropdown: 6 options verification and all operators enable Next
  - CXDV-79752: [QA][Custom Reporting] Add Filters - Enter non-numeric value in Metric filter value field, verify Next is disabled
  - CXDV-79740: [QA][Custom Reporting] Add Filters - Change logical connector between two attribute filters from and to or
  - CXDV-76477: [QA][Custom Reporting] Category Grouping
  - CXDV-76436: [QA][Custom Reporting] Filter Operators - AND/OR operators work correctly
  - CXDV-79755: [QA][Custom Reporting] Add Filters - Add maximum allowed filter rows and verify system handles limit gracefully
  - CXDV-79751: [QA][Custom Reporting] Add Filters - Add Attribute filter without selecting a value, verify Next is disabled
  - CXDV-79748: [QA][Custom Reporting] Add Filters - Remove a filter row and verify remaining rows are intact
  - CXDV-79737: [QA][Custom Reporting] Add Filters - Attribute filter with equals (=) single value and not-equals (<>) multiple values
  - CXDV-76480: [QA][Custom Reporting] Header Create and Modify - Optional field
  - CXDV-79736: [QA][Custom Reporting] Add Filters - Add single Attribute filter with equals operator and single value, verify Next enables
  - CXDV-76509: [QA][Custom Reporting] Maximum Attributes Selected
  - CXDV-79744: [QA][Custom Reporting] Add Filters - Group Selection checkbox behavior (single/multiple selection) and filter row removal
  - CXDV-79750: [QA][Custom Reporting] Add Filters - Verify metric dropdown in Metric Group shows only metrics selected in Step 2

---

## Report Template Permissions

### Reporting Template ACD Permissions (Waves team)
- Xray folder: `/CXDV_20Mar2026/Reporting Template ACD Permissions-Waves`
- Test count: 31
- Sample scenarios:
  - CXDV-24433: [Contact History Report permissions]- Verify "Contact History" report permission for Administrator role
  - CXDV-20886: [Skill Proficiency Report permissions]- Verify "Skill Proficiency" report permission for Administrator role
  - CXDV-64287: Verify Change Audit Permission for Administrator role
  - CXDV-20916: [Evaluator Analysis Report permissions]- Verify "Evaluator Analysis" report permission for Administrator
  - CXDV-24374: [Agent Contact Performance Report permissions]- Verify "Agent Contact Performance" report permission for Administrator role
  - CXDV-58937: [List of Unavailable Codes Report permissions]- Verify "List of Unavailable Codes" report permission for Administrator role
  - CXDV-25459: [ACD- QM template permissions]- Verify ACD-QM template Permissions for administrator role
  - CXDV-20883: [Abandons By Skill Report permissions]- Verify "Abandons By Skill" report permission for Administrator role
  - CXDV-41920: [List of Skills Report  permissions]- Verify List of Skills report  permission for Administrator role
  - CXDV-58945: [Agent Behavior Insights Report permissions]- Verify "Agent Behavior Insights" report permission for Administrator role
  - CXDV-58944: [Coaching Transaction Report permissions]- Verify "Coaching Transaction" report permission for Administrator role
  - CXDV-24432: [Contact States by Interval Report permissions]- Verify "Contact States by Interval" report permission for Administrator role
  - CXDV-58941: [Calibration Report permissions]- Verify "Calibration" report permission for Administrator role
  - CXDV-24373: [Agent Contact History Report permissions]- Verify "Agent Contact History" report permission for Administrator role
  - CXDV-41921: [List of Teams Report  permissions]- Verify List of Teams report  permission for Administrator role

---

## Legacy/reference report suite ("Guardians — Only for Reference")

### ClearView reports
- Xray folder: `/CXDV_20Mar2026/Guardians (Only for Reference)/ClearView reports`
- Test count: 103 (showing first 100)
- Sample scenarios:
  - CXDV-52493: Dashboards > Reports > List of Teams > Verify that global filter can be applied when doing Impersonation over own dashboard reports
  - CXDV-51788: HL - Dashboards > Reports > Verify global data filter when having a new agent configured for selected teams
  - CXDV-52507: HL - Dashboards > Reports > Verify the Confirmation behavior when switching dashboards and global filter is not saved
  - CXDV-52225: Dashboards > Reports > List of Agents Detail > Verify that Old Dashboards support and respond to global filter selections
  - CXDV-52456: Dashboards > Reports > Agent Skill Assignment > Verify that new Dashboards support and respond to global filter selections
  - CXDV-51787: HL - Dashboards > Reports > When setting Teams filter, verify that Agents displayed into the Report, belong to the team
  - CXDV-52480: Dashboards > Reports > List of Skills > RBAC - Verify the filter values when Team View configured
  - CXDV-52315: Dashboards > Reports > List of Agents Detail > RBAC - Verify the filter values when Team and Skill views are configured
  - CXDV-52454: Dashboards > Reports > Skill Proficiency > Verify that dashboards (new and existing) support and respond to global filter selections
  - CXDV-52661: Dashboards > Reports > Verify that ‘Apply' button should be enabled if new configured filters added/changed
  - CXDV-51793: HL - Dashboards > Metrics > Verify "ASA with Callback Time Exclusion" metric name and description from Metrics section
  - CXDV-52660: Dashboards > Reports > When reopening the filter panel, verify that the user sees the previously configured filters, with the 'Apply' button enabled
  - CXDV-52505: HL - Dashboards > Reports > Verify the 'Clear All' button
  - CXDV-52473: Dashboards > Reports > List of Campaigns > Verify that global filter can be applied when doing Impersonation over own dashboard reports
  - CXDV-52504: HL - Dashboards > Reports > Verify the 'Add Criteria' Button Text
  - CXDV-52488: Dashboards > Reports > List of Teams > Verify that  Reports function as expected when no filters are applied.
  - CXDV-52450: Dashboards > Reports > Skill Proficiency > RBAC - Verify the filter values when Skill View configured
  - CXDV-52457: Dashboards > Reports > Agent Skill Assignment > Verify that Old Dashboards support and respond to global filter selections
  - CXDV-52486: Dashboards > Reports > List of Teams > Verify that new Dashboards support and respond to global filter selections
  - CXDV-52485: Dashboards > Reports > List of Skills > RBAC - Verify the filter values when Team and Skill views are configured

Note: labeled "(Only for Reference)" in Xray — historical/legacy context only, not a live source of truth.

---

## Mapping to this repo's suites

| Xray folder | This repo's suite |
|---|---|
| `Dragonfly/Report Queries/*` (per-report backend query validation) | `dv-report-template/suite01`–`suite09` (2 reports per suite) |
| `Dragonfly/25.3_QA/ACD Reports`, `25.4_QA/QM Reports` | `dv-dashboard-widgets/historical/suite03` (Reports + WFM widgets) |
| `Custom Reporting`, `Reporting Template ACD Permissions-Waves` | `dv-dashboard-widgets/historical/suite02` (Common settings + permissions) |
| `Guardians/ClearView reports` | legacy — cross-check only, not actively maintained |

Not yet mapped: `dv-post-deploy-test` (suite01/suite02) and `dv-widget-api` don't have a dedicated Xray sub-folder identified in this sync — their Jira test keys are likely scattered across per-release QA folders rather than grouped by suite. Revisit if generating new tests for those suites from Xray.
