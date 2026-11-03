// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The QR code for a TOTP enrolment, rendered locally by the `qrcode` library
 * (the secret never leaves the browser for a third-party service). The SVG
 * goes into an `<img>` as a `data:` URL, which the CSP allows (`img-src
 * 'self' data: blob:`); no canvas and no inline markup injection.
 */
import QRCode from "qrcode";

const OTPAUTH_PREFIX = "otpauth://totp/";
const MAX_URI_LENGTH = 1024;

export function isOtpauthUri(value: string): boolean {
  return value.startsWith(OTPAUTH_PREFIX) && value.length <= MAX_URI_LENGTH;
}

export async function otpauthQrDataUrl(uri: string): Promise<string> {
  if (!isOtpauthUri(uri)) {
    throw new Error("not an otpauth URI");
  }
  const svg = await QRCode.toString(uri, { type: "svg", errorCorrectionLevel: "M", margin: 2 });
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

/** The base32 secret in groups of four, for typing it in by hand. */
export function groupedSecret(secret: string): string {
  return (secret.replace(/\s/g, "").match(/.{1,4}/g) ?? []).join(" ");
}
