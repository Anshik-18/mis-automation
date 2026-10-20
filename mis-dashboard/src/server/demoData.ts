/**
 * SYNTHETIC DEMO DATA - for local UI development only. Nothing here is, or resembles, real customer data.
 * Deterministic (seeded) so tests are repeatable. Dates are relative to "today" so the date presets always hit data.
 */
import { addDays } from "../lib/dates";
import type { AttributionStatus, LeadRow, Meta, PortalOption, StatusOption } from "../lib/types";

export const DEMO_STATUSES: StatusOption[] = [
  { code: "registered", label: "Registered" }, { code: "partial", label: "Partial application" },
  { code: "in_process", label: "In process" }, { code: "on_hold", label: "On hold" },
  { code: "approved", label: "Approved" }, { code: "disbursement_pending", label: "Disbursement pending" },
  { code: "disbursed", label: "Disbursed" }, { code: "closed", label: "Closed" },
  { code: "withdrawn", label: "Withdrawn" }, { code: "rejected", label: "Rejected" },
  { code: "unmapped", label: "Unmapped status" },
];

export const DEMO_PORTALS: PortalOption[] = Array.from({ length: 12 }, (_, i) => {
  const n = String(i + 1).padStart(2, "0");
  return { code: `demo_${n}`, name: `Demo Portal ${n}` };
});

export const DEMO_PARTNERS: Record<string, string> = {
  "000": "Demo Partner 000", "027": "Demo Partner 027", "27P": "Demo Partner 27P",
  "A1B": "Demo Partner A1B", "9ZK": "Demo Partner 9ZK", "0A0": "Demo Partner 0A0",
};
export const DEMO_UNREGISTERED_CODE = "ZZ9";

export const DEMO_DEFINITIONS: Meta["definitions"] = {
  approvedStatuses: ["approved", "disbursement_pending"],
  disbursedStatuses: ["disbursed"],
  rejectedStatuses: ["rejected"],
};

function mulberry32(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const STATUS_WEIGHTS: [string, number][] = [
  ["registered", 14], ["partial", 10], ["in_process", 12], ["on_hold", 3], ["approved", 9], ["disbursement_pending", 4],
  ["disbursed", 14], ["closed", 3], ["withdrawn", 4], ["rejected", 26], ["unmapped", 1],
];
const STATES: [string, string[]][] = [
  ["Maharashtra", ["Mumbai", "Pune", "Nagpur"]], ["Karnataka", ["Bengaluru", "Mysuru"]], ["Delhi", ["New Delhi"]],
  ["Tamil Nadu", ["Chennai", "Coimbatore"]], ["Gujarat", ["Ahmedabad", "Surat"]], ["Kerala", ["Kochi"]],
];
const EMPLOYMENT = ["Salaried", "Self employed", "Business", "Professional"];
const SOURCES = ["Web", "Mobile app", "Campaign", "Referral"];

function pick<T>(rng: () => number, arr: T[]): T { return arr[Math.floor(rng() * arr.length)]; }
function weighted(rng: () => number, items: [string, number][]): string {
  const total = items.reduce((s, [, w]) => s + w, 0);
  let r = rng() * total;
  for (const [v, w] of items) { if ((r -= w) < 0) return v; }
  return items[0][0];
}
const round2 = (n: number) => Math.round(n * 100) / 100;

export function generateDemoLeads(today: string, size = 6000, seed = 20261019): LeadRow[] {
  const rng = mulberry32(seed);
  const codes = Object.keys(DEMO_PARTNERS);
  const rows: LeadRow[] = [];
  for (let i = 0; i < size; i++) {
    const entry = addDays(today, -Math.floor(rng() * 150));
    const portal = pick(rng, DEMO_PORTALS);
    const r = rng();
    let attributionStatus: AttributionStatus; let code: string | null = null;
    if (r < 0.62) { attributionStatus = "resolved"; code = codes[Math.floor(Math.pow(rng(), 1.6) * codes.length)]; }
    else if (r < 0.70) { attributionStatus = "unregistered_code"; code = DEMO_UNREGISTERED_CODE; }
    else if (r < 0.80) attributionStatus = "organic";
    else attributionStatus = "unresolved";
    const status = weighted(rng, STATUS_WEIGHTS);
    const requested = (1 + Math.floor(rng() * 10)) * 10000;
    const hasApproval = ["approved", "disbursement_pending", "disbursed", "closed"].includes(status);
    const approved = hasApproval ? round2(requested * (0.7 + rng() * 0.3)) : null;
    const isDisb = status === "disbursed" || status === "closed";
    let disbursedDate: string | null = null; let disbursedAmount: number | null = null; let payout: number | null = null;
    if (isDisb) {
      const d = addDays(entry, 1 + Math.floor(rng() * 10));
      disbursedDate = d > today ? today : d;
      if (rng() > 0.1 || status === "closed") disbursedAmount = approved;   // ~10% of disbursed leads have no amount reported
      if (disbursedAmount !== null && rng() > 0.15) payout = round2(disbursedAmount * (0.01 + rng() * 0.02));
    }
    const lastTouch = disbursedDate ?? entry;
    const reportDate = addDays(lastTouch, Math.floor(rng() * 4));
    const [state, cities] = pick(rng, STATES);
    rows.push({
      leadId: String(i + 1), externalLeadId: `DEMO-${String(100000 + i)}`, idNamespace: "lead_id",
      attributionCode: code, attributionStatus, partnerName: code && DEMO_PARTNERS[code] ? DEMO_PARTNERS[code] : null,
      portalCode: portal.code, portalName: portal.name, status,
      statusRaw: status.replace(/_/g, " ").toUpperCase(),
      statusReason: status === "rejected" ? pick(rng, ["Low score", "Policy mismatch", "Incomplete KYC"]) : null,
      leadEntryDate: entry, disbursedDate, reportDate: reportDate > today ? today : reportDate,
      requestedLoanAmount: requested, approvedAmount: approved, disbursedAmount, payoutAmount: payout,
      state, city: pick(rng, cities), employmentType: pick(rng, EMPLOYMENT), leadSource: pick(rng, SOURCES),
      lastObservedAt: `${reportDate > today ? today : reportDate}T02:00:00.000Z`,
    });
  }
  // Edge cases for export/escaping tests. Entered "today" so they are visible in the default view.
  const edge = (idx: number, patch: Partial<LeadRow>) => { Object.assign(rows[idx], { leadEntryDate: today, reportDate: today, ...patch }); };
  edge(0, { externalLeadId: "=SUM(1,1)", city: 'Mumbai, "East"\nZone', state: "Maharashtra", attributionCode: "027", attributionStatus: "resolved", partnerName: DEMO_PARTNERS["027"] });
  edge(1, { externalLeadId: "DEMO-UNI", city: "Pune – पुणे", state: "Maharashtra", attributionCode: "27P", attributionStatus: "resolved", partnerName: DEMO_PARTNERS["27P"] });
  edge(2, { externalLeadId: "@cmd|' /C calc'!A0", attributionCode: "000", attributionStatus: "resolved", partnerName: DEMO_PARTNERS["000"] });
  return rows;
}
