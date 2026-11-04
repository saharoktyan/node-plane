//! The little PEM, DER and base64 enrollment needs: the agent's P-256 key
//! and the public key inside the certificate the controller issues.

const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

pub(crate) fn base64(data: &[u8]) -> String {
    let mut text = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let bytes = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let group = u32::from(bytes[0]) << 16 | u32::from(bytes[1]) << 8 | u32::from(bytes[2]);
        for index in 0..4 {
            if index <= chunk.len() {
                text.push(char::from(
                    ALPHABET[(group >> (18 - 6 * index) & 63) as usize],
                ));
            } else {
                text.push('=');
            }
        }
    }
    text
}

pub(crate) fn unbase64(text: &str) -> Option<Vec<u8>> {
    let text = text.trim_end_matches('=');
    let mut data = Vec::with_capacity(text.len() * 3 / 4);
    let (mut group, mut bits) = (0u32, 0);
    for byte in text.bytes() {
        let value = ALPHABET.iter().position(|c| *c == byte)? as u32;
        group = group << 6 | value;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            data.push((group >> bits) as u8);
        }
    }
    Some(data)
}

pub(crate) fn pem(label: &str, der: &[u8]) -> String {
    let body = base64(der);
    let mut text = format!("-----BEGIN {label}-----\n");
    for line in body.as_bytes().chunks(64) {
        text.push_str(std::str::from_utf8(line).expect("base64 is ASCII"));
        text.push('\n');
    }
    text.push_str(&format!("-----END {label}-----\n"));
    text
}

/// The first `label` block of a PEM text.
pub(crate) fn unpem(label: &str, text: &str) -> Option<Vec<u8>> {
    let begin = format!("-----BEGIN {label}-----");
    let end = format!("-----END {label}-----");
    let start = text.find(&begin)? + begin.len();
    let stop = start + text[start..].find(&end)?;
    let body: String = text[start..stop].split_whitespace().collect();
    unbase64(&body)
}

/// A DER element's tag, the whole element, its content and what follows.
type Element<'a> = (u8, &'a [u8], &'a [u8], &'a [u8]);

fn element(data: &[u8]) -> Option<Element<'_>> {
    let (&tag, rest) = data.split_first()?;
    let (&first, rest) = rest.split_first()?;
    let (length, rest) = if first < 0x80 {
        (usize::from(first), rest)
    } else {
        let count = usize::from(first & 0x7f);
        if count == 0 || count > 4 || rest.len() < count {
            return None;
        }
        let length = rest[..count]
            .iter()
            .fold(0usize, |length, byte| length << 8 | usize::from(*byte));
        (length, &rest[count..])
    };
    if rest.len() < length {
        return None;
    }
    let header = data.len() - rest.len();
    Some((
        tag,
        &data[..header + length],
        &rest[..length],
        &rest[length..],
    ))
}

/// The SubjectPublicKeyInfo element of a DER certificate.
pub(crate) fn certificate_key(der: &[u8]) -> Option<&[u8]> {
    let (0x30, _, certificate, _) = element(der)? else {
        return None;
    };
    let (0x30, _, mut fields, _) = element(certificate)? else {
        return None;
    };
    // version [0] is optional; then serial, signature, issuer, validity,
    // subject and the key.
    if fields.first() == Some(&0xa0) {
        fields = element(fields)?.3;
    }
    for expected in [0x02, 0x30, 0x30, 0x30, 0x30] {
        let (tag, _, _, rest) = element(fields)?;
        if tag != expected {
            return None;
        }
        fields = rest;
    }
    match element(fields)? {
        (0x30, whole, _, _) => Some(whole),
        _ => None,
    }
}

/// SubjectPublicKeyInfo of an uncompressed P-256 point.
pub(crate) fn p256_key(point: &[u8]) -> Vec<u8> {
    let mut der = vec![
        0x30, 0x59, 0x30, 0x13, 0x06, 0x07, 0x2a, 0x86, 0x48, 0xce, 0x3d, 0x02, 0x01, 0x06, 0x08,
        0x2a, 0x86, 0x48, 0xce, 0x3d, 0x03, 0x01, 0x07, 0x03, 0x42, 0x00,
    ];
    der.extend_from_slice(point);
    der
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64_round_trips_every_padding() {
        for data in [&b""[..], b"f", b"fo", b"foo", b"foob", b"\xff\x00\x10"] {
            assert_eq!(unbase64(&base64(data)).unwrap(), data);
        }
        assert_eq!(base64(b"fo"), "Zm8=");
        assert!(unbase64("Zm8*").is_none());
    }

    #[test]
    fn pem_blocks_round_trip() {
        let der = (0..=255u8).collect::<Vec<_>>();
        let text = format!("junk\n{}more", pem("CERTIFICATE", &der));
        assert_eq!(unpem("CERTIFICATE", &text).unwrap(), der);
        assert!(unpem("PRIVATE KEY", &text).is_none());
    }

    #[test]
    fn long_lengths_and_truncation() {
        let mut long = vec![0x04, 0x81, 200];
        long.extend([7; 200]);
        let (tag, whole, content, rest) = element(&long).unwrap();
        assert_eq!(
            (tag, whole.len(), content.len(), rest.len()),
            (4, 203, 200, 0)
        );
        assert!(element(&long[..100]).is_none());
        assert!(certificate_key(&[0x30, 0x00]).is_none());
    }
}
