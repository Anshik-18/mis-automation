export const CODE_RE = /^[0-9A-Z]{3}$/;

/** Trim + upper-case. The value is always TEXT: '027' must never pass through Number(). */
export function normalizeCode(input: string): string {
  return (input ?? "").trim().toUpperCase();
}

export type CodeCheck = { ok: true; value: string } | { ok: false; value: string; error: string };

/** Empty is valid (= all partners). Otherwise exactly three ASCII letters/digits. */
export function checkCode(input: string): CodeCheck {
  const value = normalizeCode(input);
  if (value === "") return { ok: true, value };
  if (value.length !== 3) return { ok: false, value, error: "Enter exactly 3 characters, for example 027 or 27P." };
  if (!CODE_RE.test(value)) return { ok: false, value, error: "Use only letters A–Z and digits 0–9." };
  return { ok: true, value };
}
