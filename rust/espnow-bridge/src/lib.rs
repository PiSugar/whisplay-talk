use std::collections::HashMap;
use std::time::{Duration, Instant};

pub const ESPRESSIF_OUI: [u8; 3] = [0x18, 0xfe, 0x34];
pub const BROADCAST: [u8; 6] = [0xff; 6];
pub const MAX_PAYLOAD: usize = 250;
pub const CONTROL_REPEATS: usize = 7;
// Each application packet already carries the previous Opus frame and Opus
// in-band FEC. BCM43430 pcap injection blocks until firmware accepts a frame,
// so RF-level repeats reduce throughput below the required 25 packets/s.
pub const DEFAULT_AUDIO_REPEATS: usize = 1;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedFrame {
    pub source: [u8; 6],
    pub payload: Vec<u8>,
}

pub fn build_espnow_frame(
    source: [u8; 6],
    destination: [u8; 6],
    payload: &[u8],
    sequence: u16,
    rate_500_kbps: u8,
) -> Result<Vec<u8>, &'static str> {
    if payload.len() > MAX_PAYLOAD {
        return Err("ESP-NOW v1 payload exceeds 250 bytes");
    }

    let mut frame = Vec::with_capacity(9 + 24 + 15 + payload.len());
    frame.extend_from_slice(&[0, 0, 9, 0, 1 << 2, 0, 0, 0, rate_500_kbps]);
    frame.extend_from_slice(&[0xd0, 0, 0, 0]);
    frame.extend_from_slice(&destination);
    frame.extend_from_slice(&source);
    frame.extend_from_slice(&BROADCAST);
    frame.extend_from_slice(&((sequence & 0x0fff) << 4).to_le_bytes());
    frame.push(0x7f);
    frame.extend_from_slice(&ESPRESSIF_OUI);
    frame.extend_from_slice(&(sequence as u32).to_le_bytes());
    frame.push(0xdd);
    frame.push((5 + payload.len()) as u8);
    frame.extend_from_slice(&ESPRESSIF_OUI);
    frame.extend_from_slice(&[0x04, 0x01]);
    frame.extend_from_slice(payload);
    Ok(frame)
}

pub fn parse_espnow_frame(frame: &[u8]) -> Option<ParsedFrame> {
    if frame.len() < 4 {
        return None;
    }
    let radiotap_len = u16::from_le_bytes([frame[2], frame[3]]) as usize;
    let action_offset = radiotap_len.checked_add(24)?;
    if radiotap_len < 8 || frame.len() < action_offset + 15 {
        return None;
    }
    let dot11 = frame.get(radiotap_len..action_offset)?;
    let frame_control = u16::from_le_bytes([dot11[0], dot11[1]]);
    if frame_control & 0x00fc != 0x00d0 {
        return None;
    }
    let source: [u8; 6] = dot11.get(10..16)?.try_into().ok()?;
    let action = frame.get(action_offset..)?;
    if action.get(..4)? != [0x7f, ESPRESSIF_OUI[0], ESPRESSIF_OUI[1], ESPRESSIF_OUI[2]] {
        return None;
    }
    let element = action.get(8..)?;
    if element.len() < 7 || element[0] != 0xdd {
        return None;
    }
    let element_len = element[1] as usize;
    if element_len < 5 || element.len() < element_len + 2 {
        return None;
    }
    if element[2..5] != ESPRESSIF_OUI || element[5] != 0x04 || element[6] != 0x01 {
        return None;
    }
    let payload = element.get(7..2 + element_len)?.to_vec();
    if payload.len() > MAX_PAYLOAD {
        return None;
    }
    Some(ParsedFrame { source, payload })
}

pub fn parse_radiotap_signal(frame: &[u8]) -> Option<i8> {
    if frame.len() < 8 || frame[0] != 0 {
        return None;
    }
    let header_len = u16::from_le_bytes([frame[2], frame[3]]) as usize;
    let present = u32::from_le_bytes(frame[4..8].try_into().ok()?);
    if header_len > frame.len() || present & (1 << 31) != 0 {
        return None;
    }
    let fields = [(8usize, 8usize), (1, 1), (1, 1), (2, 4), (2, 2), (1, 1)];
    let mut offset = 8usize;
    for (index, (alignment, size)) in fields.into_iter().enumerate() {
        if present & (1 << index) == 0 {
            continue;
        }
        offset = (offset + alignment - 1) & !(alignment - 1);
        if offset + size > header_len {
            return None;
        }
        if index == 5 {
            return Some(frame[offset] as i8);
        }
        offset += size;
    }
    None
}

pub fn parse_iw_channel(output: &str) -> Option<u8> {
    output.lines().find_map(|line| {
        let mut fields = line.split_whitespace();
        (fields.next()? == "channel")
            .then(|| fields.next()?.parse::<u8>().ok())
            .flatten()
    })
}

pub fn link_is_associated(output: &str) -> bool {
    output
        .lines()
        .any(|line| line.trim_start().starts_with("Connected to "))
}

pub fn select_audio_repeats<I>(peer_rssi: I) -> usize
where
    I: IntoIterator<Item = f64>,
{
    let _ = peer_rssi.into_iter().reduce(f64::min);
    DEFAULT_AUDIO_REPEATS
}

pub struct RecentFrameCache {
    window: Duration,
    seen: HashMap<([u8; 6], Vec<u8>), Instant>,
}

impl RecentFrameCache {
    pub fn new(window: Duration) -> Self {
        Self {
            window,
            seen: HashMap::new(),
        }
    }

    pub fn is_duplicate(&mut self, source: [u8; 6], payload: &[u8], now: Instant) -> bool {
        let key = (source, payload.to_vec());
        let previous = self.seen.insert(key, now);
        if self.seen.len() > 256 {
            let cutoff = now.checked_sub(self.window).unwrap_or(now);
            self.seen.retain(|_, seen_at| *seen_at >= cutoff);
        }
        previous.is_some_and(|seen_at| now.duration_since(seen_at) < self.window)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frame_round_trip() {
        let source = [0xb8, 0x27, 0xeb, 1, 2, 3];
        let payload = b"WD01whisplay-talk-test";
        let frame = build_espnow_frame(source, BROADCAST, payload, 123, 2).unwrap();
        assert_eq!(
            parse_espnow_frame(&frame),
            Some(ParsedFrame {
                source,
                payload: payload.to_vec()
            })
        );
    }

    #[test]
    fn rejects_oversized_payload() {
        assert!(build_espnow_frame([0; 6], BROADCAST, &[0; 251], 1, 2).is_err());
    }

    #[test]
    fn adaptive_repeats_follow_weakest_peer() {
        assert_eq!(select_audio_repeats([]), 1);
        assert_eq!(select_audio_repeats([-40.0, -49.0]), 1);
        assert_eq!(select_audio_repeats([-50.0, -64.0]), 1);
        assert_eq!(select_audio_repeats([-60.0, -74.0]), 1);
        assert_eq!(select_audio_repeats([-90.0]), 1);
    }

    #[test]
    fn duplicate_window_expires() {
        let source = [1, 2, 3, 4, 5, 6];
        let start = Instant::now();
        let mut cache = RecentFrameCache::new(Duration::from_millis(150));
        assert!(!cache.is_duplicate(source, b"packet", start));
        assert!(cache.is_duplicate(source, b"packet", start + Duration::from_millis(100)));
        assert!(!cache.is_duplicate(source, b"packet", start + Duration::from_millis(300)));
    }
}
