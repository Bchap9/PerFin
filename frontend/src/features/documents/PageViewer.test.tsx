// SPDX-License-Identifier: AGPL-3.0-or-later
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";

import { json, mockApi } from "../auth/testing";
import { PageViewer } from "./PageViewer";

const REGIONS = {
  document_id: "d-1",
  page: 1,
  page_count: 1,
  width: 612,
  height: 792,
  unit: "pt",
  preview: null,
  text_included: true,
  text_source: "text_layer",
  words: [
    { bbox: [10, 10, 60, 24], conf: 0.9, line: 1, text: "Rent" },
    { bbox: [100, 10, 160, 24], conf: 0.9, line: 1, text: "1200.00" },
  ],
  words_truncated: false,
};

/** The page is drawn at half size, 100 px from the left and 50 px from the top of the screen. */
const SCREEN_CTM = { a: 0.5, b: 0, c: 0, d: 0.5, e: 100, f: 50 };

function screenX(pageX: number): number {
  return SCREEN_CTM.a * pageX + SCREEN_CTM.e;
}

function screenY(pageY: number): number {
  return SCREEN_CTM.d * pageY + SCREEN_CTM.f;
}

function renderViewer(onSelect: (page: number, bbox: number[]) => void) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <PageViewer
        documentId="d-1"
        onSelect={(page, bbox) => {
          onSelect(page, [...bbox]);
        }}
      />
    </QueryClientProvider>,
  );
}

/** jsdom has no PointerEvent; pointer events then carry no coordinates. */
const FIRE = new Map([
  ["pointerDown", fireEvent.pointerDown],
  ["pointerMove", fireEvent.pointerMove],
  ["pointerUp", fireEvent.pointerUp],
] as const);

function pointer(target: Element, type: "pointerDown" | "pointerMove" | "pointerUp", pageX: number, pageY: number): void {
  FIRE.get(type)?.(target, { clientX: screenX(pageX), clientY: screenY(pageY), pointerId: 1, button: 0 });
}

describe("page viewer selection on the SVG overlay", () => {
  const original = Object.getOwnPropertyDescriptor(SVGElement.prototype, "getScreenCTM");
  const hadPointerEvent = "PointerEvent" in window;

  beforeEach(() => {
    Object.defineProperty(SVGElement.prototype, "getScreenCTM", {
      configurable: true,
      value: () => SCREEN_CTM,
    });
    if (!hadPointerEvent) {
      // Pointer events with coordinates, as a browser sends them.
      vi.stubGlobal("PointerEvent", MouseEvent);
    }
    mockApi({ "GET /api/v1/documents/:id/regions": () => json(REGIONS) });
  });

  afterEach(() => {
    if (original === undefined) {
      Reflect.deleteProperty(SVGElement.prototype, "getScreenCTM");
    } else {
      Object.defineProperty(SVGElement.prototype, "getScreenCTM", original);
    }
  });

  it("a click on a word picks that word, through the overlay's screen matrix", async () => {
    const picked: [number, number[]][] = [];
    renderViewer((page, bbox) => picked.push([page, bbox]));
    const overlay = await screen.findByTestId("page-overlay");
    pointer(overlay, "pointerDown", 120, 15);
    pointer(overlay, "pointerUp", 120, 15);
    expect(picked).toEqual([[1, [100, 10, 160, 24]]]);
  });

  it("a drag draws a box in page units and selects the words inside it", async () => {
    const picked: [number, number[]][] = [];
    renderViewer((page, bbox) => picked.push([page, bbox]));
    const overlay = await screen.findByTestId("page-overlay");
    pointer(overlay, "pointerDown", 70, 30);
    pointer(overlay, "pointerMove", 5, 4);
    const drag = overlay.querySelector(".page-viewer__box--drag");
    expect(drag?.tagName.toLowerCase()).toBe("rect");
    expect(drag).toHaveAttribute("x", "5");
    expect(drag).toHaveAttribute("width", "65");
    pointer(overlay, "pointerUp", 5, 4);
    expect(picked).toEqual([[1, [5, 4, 70, 30]]]);
    expect(overlay.querySelector(".page-viewer__box--drag")).toBeNull();
  });

  it("selects nothing when the overlay has no screen matrix", async () => {
    Object.defineProperty(SVGElement.prototype, "getScreenCTM", { configurable: true, value: () => null });
    const picked: [number, number[]][] = [];
    renderViewer((page, bbox) => picked.push([page, bbox]));
    const overlay = await screen.findByTestId("page-overlay");
    pointer(overlay, "pointerDown", 120, 15);
    pointer(overlay, "pointerUp", 120, 15);
    expect(picked).toEqual([]);
  });
});
