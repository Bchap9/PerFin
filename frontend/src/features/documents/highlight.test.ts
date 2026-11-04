// SPDX-License-Identifier: AGPL-3.0-or-later
import { boxStyle, onPage, pagesWithBoxes, wordInside } from "./highlight";

const LETTER = { width: 612, height: 792 };

describe("boxStyle", () => {
  it("places a box as percentages of the page", () => {
    expect(boxStyle([61.2, 79.2, 306, 396], LETTER)).toEqual({
      left: "10%",
      top: "10%",
      width: "40%",
      height: "40%",
    });
  });

  it("does not depend on the rendered size (pixel pages too)", () => {
    const pixels = { width: 1700, height: 2200 };
    expect(boxStyle([170, 220, 850, 1100], pixels)).toEqual({
      left: "10%",
      top: "10%",
      width: "40%",
      height: "40%",
    });
  });

  it("clamps a box that runs off the page and orders flipped corners", () => {
    expect(boxStyle([700, 396, 306, 900], LETTER)).toEqual({
      left: "50%",
      top: "50%",
      width: "50%",
      height: "50%",
    });
  });

  it("refuses malformed, empty and off-page boxes", () => {
    expect(boxStyle([1, 2, 3], LETTER)).toBeNull();
    expect(boxStyle([1, 2, 3, Number.NaN], LETTER)).toBeNull();
    expect(boxStyle([10, 10, 10, 50], LETTER)).toBeNull();
    expect(boxStyle([700, 10, 800, 50], LETTER)).toBeNull();
    expect(boxStyle([0, 0, 10, 10], { width: 0, height: 792 })).toBeNull();
  });
});

describe("box selection", () => {
  const boxes = [
    { page: 2, bbox: [0, 0, 1, 1] },
    { page: 1, bbox: [0, 0, 1, 1] },
    { page: 2, bbox: [1, 1, 2, 2] },
  ];
  it("filters by page and lists pages once", () => {
    expect(onPage(boxes, 2)).toHaveLength(2);
    expect(pagesWithBoxes(boxes)).toEqual([1, 2]);
  });
  it("marks a word by its centre", () => {
    expect(wordInside([10, 10, 20, 20], [0, 0, 100, 100])).toBe(true);
    expect(wordInside([95, 95, 130, 130], [0, 0, 100, 100])).toBe(false);
    expect(wordInside([1], [0, 0, 1, 1])).toBe(false);
  });
});
