import type { TypedValue } from "./types";

export const shortSha = (sha: string) => sha.slice(0, 12);

/** A typed value as text: SQL NULL, an absent side and every scalar are told apart. */
export function formatValue(value: TypedValue | null): string {
  if (value === null) return "(absent)";
  if (value.type === "null") return "NULL";
  if (value.value === undefined) return value.type;
  return typeof value.value === "string" ? value.value : JSON.stringify(value.value);
}

export function formatKey(key: TypedValue[]): string {
  return key.map((part) => formatValue(part)).join(", ");
}

export function formatTime(iso: string): string {
  return new Date(iso).toISOString().replace("T", " ").replace(/\.\d+Z$/, " UTC");
}
