// SQL from a pull request, readable but inert: each token is React text inside a span, never
// HTML, and together the tokens are exactly the original text.

const KEYWORDS = new Set(
  (
    "select from where join left right inner outer full cross on using group by order having as and or not in " +
    "is null case when then else end with union all distinct limit offset insert into values update set delete " +
    "create table alter drop exists between like ilike asc desc over partition filter cast true false returning"
  ).split(" "),
);
const FUNCTIONS = new Set("sum count avg min max coalesce nullif round floor ceil trunc row_number now".split(" "));

// Every alternative consumes at least one character, and the last takes any one, so the tokens
// cover the whole text.
const TOKEN =
  /(--[^\n]*|\/\*[\s\S]*?\*\/)|('(?:[^']|'')*'?)|("(?:[^"]|"")*"?)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_$]*)|(\s+)|([\s\S])/g;

export type SqlToken = { kind: "comment" | "string" | "identifier" | "number" | "keyword" | "function" | "plain"; text: string };

export function tokenize(sql: string): SqlToken[] {
  const tokens: SqlToken[] = [];
  for (const match of sql.matchAll(TOKEN)) {
    const [text, comment, string, quoted, number, word] = match;
    if (comment !== undefined) tokens.push({ kind: "comment", text });
    else if (string !== undefined) tokens.push({ kind: "string", text });
    else if (quoted !== undefined) tokens.push({ kind: "identifier", text });
    else if (number !== undefined) tokens.push({ kind: "number", text });
    else if (word !== undefined) {
      const lower = word.toLowerCase();
      tokens.push({ kind: KEYWORDS.has(lower) ? "keyword" : FUNCTIONS.has(lower) ? "function" : "plain", text });
    } else tokens.push({ kind: "plain", text });
  }
  return tokens;
}

export function Sql({ sql }: { sql: string }) {
  return (
    <pre className="sql">
      {tokenize(sql).map((token, index) =>
        token.kind === "plain" ? token.text : (
          <span key={index} className={`sql-${token.kind}`}>
            {token.text}
          </span>
        ),
      )}
    </pre>
  );
}
