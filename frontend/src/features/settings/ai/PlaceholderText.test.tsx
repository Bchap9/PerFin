// SPDX-License-Identifier: AGPL-3.0-or-later
import { render } from "@testing-library/react";

import { JsonWithPlaceholders, PlaceholderText } from "./PlaceholderText";

const IMG = '<img src=x onerror="window.__pwned=1">';
const SCRIPT = "<script>window.__pwned = 1</script>";
const EVIL = [
  IMG,
  SCRIPT,
  '"><svg onload=window.__pwned=1>',
  "<b>bold</b> &lt;i&gt; &amp;",
  "java" + "script:alert(1)",
];

describe("PlaceholderText", () => {
  it("styles each placeholder as a mark", () => {
    const { container } = render(<PlaceholderText text="Paid [MEMBER] at [ADDRESS]" />);
    const marks = [...container.querySelectorAll("mark.ai-placeholder")];
    expect(marks.map((mark) => mark.textContent)).toEqual(["[MEMBER]", "[ADDRESS]"]);
    expect(marks.map((mark) => mark.getAttribute("data-placeholder"))).toEqual(["[MEMBER]", "[ADDRESS]"]);
    expect(container.textContent).toBe("Paid [MEMBER] at [ADDRESS]");
  });

  it.each(EVIL)("never turns %s into elements", (evil) => {
    const text = `${evil}[ACCT]${evil}`;
    const { container } = render(<PlaceholderText text={text} />);
    expect(container.querySelector("img, script, svg, b, i")).toBeNull();
    expect(container.querySelectorAll("*").length).toBe(container.querySelectorAll("span, mark").length);
    expect(container.textContent).toBe(text);
    expect((window as unknown as Record<string, unknown>)["__pwned"]).toBeUndefined();
  });
});

describe("JsonWithPlaceholders", () => {
  it("renders values, keys and nesting as text with highlighted placeholders", () => {
    const value = { payee: "Cafe [MEMBER]", items: [1, null, true, "[EMAIL]"], empty: {}, none: [] };
    const { container } = render(<JsonWithPlaceholders value={value} label="sample" />);
    const text = container.textContent;
    expect(text).toContain('"payee": "Cafe [MEMBER]"');
    expect(text).toContain('"empty": {}');
    expect(text).toContain('"none": []');
    expect([...container.querySelectorAll("mark")].map((mark) => mark.textContent)).toEqual(["[MEMBER]", "[EMAIL]"]);
  });

  it("does not inject HTML from malicious values or keys", () => {
    const value = { [IMG]: EVIL, nested: { text: `${SCRIPT}[PHONE]` } };
    const { container } = render(<JsonWithPlaceholders value={value} label="evil" />);
    expect(container.querySelector("img, script, svg, b, i")).toBeNull();
    expect(container.textContent).toContain("<img src=x");
    expect(container.textContent).toContain("<script>window.__pwned = 1</script>");
    expect((window as unknown as Record<string, unknown>)["__pwned"]).toBeUndefined();
  });

  it("stops at a safe depth", () => {
    let deep: unknown = "leaf";
    for (let i = 0; i < 30; i += 1) {
      deep = { next: deep };
    }
    const { container } = render(<JsonWithPlaceholders value={deep} label="deep" />);
    expect(container.textContent).toContain("…");
  });
});
