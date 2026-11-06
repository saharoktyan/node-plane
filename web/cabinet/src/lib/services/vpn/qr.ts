import qrcode from 'qrcode-generator';

/** A configuration's QR code as SVG, drawn in the page, or null when the
 * text does not fit one. AmneziaVPN scans its key without the scheme. */
export function qr(protocol: string, transport: string, content: string): string | null {
  const payload = protocol === 'awg' && transport === 'vpn' ? content.replace(/^vpn:\/\//, '') : content;
  if (new TextEncoder().encode(payload).length > 2500) return null;
  try {
    const code = qrcode(0, 'L');
    code.addData(payload);
    code.make();
    return code.createSvgTag({ cellSize: 4, margin: 4, scalable: true });
  } catch {
    return null;
  }
}
