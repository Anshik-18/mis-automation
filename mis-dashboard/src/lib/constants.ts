import type { DateField, DatePreset, AttributionStatus } from "./types";

export const BUSINESS_TZ = "Asia/Kolkata";
export const PAGE_SIZES = [10, 25, 50, 100] as const;
export const DEFAULT_PAGE_SIZE = 25;
export const DEFAULT_PRESET: DatePreset = "last7";

export const DATE_FIELD_LABEL: Record<DateField, string> = {
  entry: "Lead Entry Date",
  disbursement: "Disbursement Date",
  report: "Report Date",
};
export const DATE_FIELD_HELP: Record<DateField, string> = {
  entry: "The date the lead was created at the portal.",
  disbursement: "The date the loan was disbursed. Leads without one are excluded.",
  report: "The report date (from the file name) of the newest file that touched the lead.",
};
export const PRESET_LABEL: Record<DatePreset, string> = {
  today: "Today", yesterday: "Yesterday", last7: "Last 7 days",
  thisMonth: "This month", prevMonth: "Previous month", custom: "Custom range",
};
export const PRESET_ORDER: DatePreset[] = ["today", "yesterday", "last7", "thisMonth", "prevMonth", "custom"];

export const ATTRIBUTION_STATUS_LABEL: Record<AttributionStatus, string> = {
  resolved: "Registered partner",
  unregistered_code: "Unregistered code",
  organic: "Organic",
  unresolved: "Unresolved",
};

export const CONVERSION_FORMULA =
  "Conversion rate = distinct leads with status Disbursed ÷ distinct leads in the filtered set × 100.";
