// The schemas are generated from the Python models; these tests prove that a strict,
// independent JSON Schema implementation accepts them and the documents Python produces.
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { Ajv2020 } from "ajv/dist/2020.js";
import addFormatsModule from "ajv-formats";
import { describe, expect, it } from "vitest";

const addFormats = addFormatsModule.default;
const root = join(dirname(fileURLToPath(import.meta.url)), "..");

const readJson = (...segments: string[]): unknown =>
  JSON.parse(readFileSync(join(root, ...segments), "utf8"));

const modelOf = (file: string): string => file.split(".")[0] ?? file;

const schemaFiles = readdirSync(join(root, "schemas")).filter((f) => f.endsWith(".schema.json"));
const exampleFiles = readdirSync(join(root, "examples")).filter((f) => f.endsWith(".json"));

function compile(model: string) {
  const ajv = new Ajv2020({ strict: true, allErrors: true });
  addFormats(ajv);
  return ajv.compile(readJson("schemas", `${model}.schema.json`) as object);
}

describe("contract JSON Schemas", () => {
  it("exist", () => {
    expect(schemaFiles.length).toBeGreaterThan(0);
  });

  it.each(schemaFiles)("%s compiles under Ajv strict mode", (file) => {
    expect(() => compile(modelOf(file))).not.toThrow();
  });
});

describe("example documents", () => {
  it.each(exampleFiles)("%s is valid against its schema", (file) => {
    const validate = compile(modelOf(file));
    const valid = validate(readJson("examples", file));
    expect(validate.errors ?? []).toEqual([]);
    expect(valid).toBe(true);
  });
});

describe("monetary values", () => {
  type CheckResultDocument = { mismatch_examples: { observed: { value: unknown } }[] };

  const withObserved = (value: unknown): CheckResultDocument => {
    const document = readJson("examples", "CheckResult.f1-detected.json") as CheckResultDocument;
    const observed = document.mismatch_examples[0]?.observed;
    expect(observed).toBeDefined();
    if (observed) observed.value = value;
    return document;
  };

  it("rejects an amount sent as a JSON number", () => {
    expect(compile("CheckResult")(withObserved(99.98))).toBe(false);
  });

  it.each(["abc", "49,99", ""])("rejects the non-decimal string %j", (value) => {
    expect(compile("CheckResult")(withObserved(value))).toBe(false);
  });

  it.each(["99.98", "-0", "0E-8"])("accepts the decimal string %j", (value) => {
    expect(compile("CheckResult")(withObserved(value))).toBe(true);
  });
});
