#![cfg(target_os = "linux")]

use libc::c_int;
use std::collections::{HashMap, HashSet};
use std::ffi::CString;
use std::fs;
use std::io;
use std::mem;
use std::os::fd::RawFd;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixDatagram;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use whisplay_espnow_bridge::{
    build_espnow_frame, link_is_associated, parse_espnow_frame, parse_iw_channel,
    parse_radiotap_signal, select_audio_repeats, RecentFrameCache, BROADCAST, CONTROL_REPEATS,
    MAX_PAYLOAD,
};

const REGISTER: u8 = b'R';
const TRANSMIT: u8 = b'T';
const FRAME: u8 = b'F';
const STATUS: u8 = b'S';
const SET_CHANNEL: u8 = b'C';
const AUTO_CHANNEL: u8 = b'A';
const MODE_AUTO: u8 = b'A';
const MODE_FIXED: u8 = b'F';
const MODE_SWITCHING: u8 = b'S';
const RATE_1_MBPS: u8 = 2;
// Keep all copies within one 40 ms audio period even at the seven-copy edge
// profile, while retaining a little time diversity against collisions.
const REPEAT_INTERVAL: Duration = Duration::from_millis(3);
const RSSI_FRESH: Duration = Duration::from_secs(15);
const STATS_INTERVAL: Duration = Duration::from_secs(10);
const INJECTOR_REFRESH: Duration = Duration::from_secs(30);
const OFFLINE_GRACE: Duration = Duration::from_secs(12);
const OFFLINE_PEER_HOLD: Duration = Duration::from_secs(12);
const OFFLINE_SCAN_SLOT: Duration = Duration::from_secs(3);
const WIFI_RETRY_INTERVAL: Duration = Duration::from_secs(60);
const WIFI_RETRY_WINDOW: Duration = Duration::from_secs(4);
const WIFI_RECONNECT_WINDOW: Duration = Duration::from_secs(20);
const AUDIO_ACTIVITY_HOLD: Duration = Duration::from_secs(5);
const FIXED_ANNOUNCE_INTERVAL: Duration = Duration::from_secs(1);
const CHANNEL_SWITCH_SETTLE: Duration = Duration::from_millis(350);

static RUNNING: AtomicBool = AtomicBool::new(true);

extern "C" fn stop_signal(_: c_int) {
    RUNNING.store(false, Ordering::SeqCst);
}

fn log(level: &str, message: impl AsRef<str>) {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    eprintln!("{now:.3} {level} espnow-bridge-rs: {}", message.as_ref());
}

fn format_mac(mac: &[u8; 6]) -> String {
    mac.iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<Vec<_>>()
        .join(":")
}

fn parse_mac(value: &str) -> Result<[u8; 6], String> {
    let bytes = value
        .trim()
        .split(':')
        .map(|part| u8::from_str_radix(part, 16).map_err(|error| error.to_string()))
        .collect::<Result<Vec<_>, _>>()?;
    bytes
        .try_into()
        .map_err(|_| "MAC address must contain 6 octets".to_string())
}

struct RadioInjector(RawFd);

impl RadioInjector {
    fn open(interface: &str) -> Result<Self, String> {
        let protocol = (libc::ETH_P_ALL as u16).to_be();
        let fd = unsafe { libc::socket(libc::AF_PACKET, libc::SOCK_RAW, protocol as c_int) };
        if fd < 0 {
            return Err(io::Error::last_os_error().to_string());
        }
        let interface = CString::new(interface).map_err(|error| error.to_string())?;
        let index = unsafe { libc::if_nametoindex(interface.as_ptr()) };
        if index == 0 {
            unsafe { libc::close(fd) };
            return Err(io::Error::last_os_error().to_string());
        }
        let mut address: libc::sockaddr_ll = unsafe { mem::zeroed() };
        address.sll_family = libc::AF_PACKET as u16;
        address.sll_protocol = protocol;
        address.sll_ifindex = index as c_int;
        let result = unsafe {
            libc::bind(
                fd,
                (&address as *const libc::sockaddr_ll).cast(),
                mem::size_of::<libc::sockaddr_ll>() as libc::socklen_t,
            )
        };
        if result < 0 {
            let error = io::Error::last_os_error();
            unsafe { libc::close(fd) };
            return Err(error.to_string());
        }
        Ok(Self(fd))
    }

    fn send(&mut self, frame: &[u8]) -> Result<(), String> {
        let written = unsafe {
            libc::send(
                self.0,
                frame.as_ptr().cast(),
                frame.len(),
                libc::MSG_DONTWAIT,
            )
        };
        if written != frame.len() as isize {
            return Err(format!(
                "raw injection wrote {written}/{} bytes: {}",
                frame.len(),
                io::Error::last_os_error(),
            ));
        }
        Ok(())
    }
}

impl Drop for RadioInjector {
    fn drop(&mut self) {
        unsafe { libc::close(self.0) };
    }
}

struct PacketSocket(RawFd);

impl PacketSocket {
    fn bind(interface: &str) -> io::Result<Self> {
        let protocol = (libc::ETH_P_ALL as u16).to_be();
        let fd = unsafe { libc::socket(libc::AF_PACKET, libc::SOCK_RAW, protocol as c_int) };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        let interface =
            CString::new(interface).map_err(|_| io::Error::from(io::ErrorKind::InvalidInput))?;
        let index = unsafe { libc::if_nametoindex(interface.as_ptr()) };
        if index == 0 {
            unsafe { libc::close(fd) };
            return Err(io::Error::last_os_error());
        }
        let mut address: libc::sockaddr_ll = unsafe { mem::zeroed() };
        address.sll_family = libc::AF_PACKET as u16;
        address.sll_protocol = protocol;
        address.sll_ifindex = index as c_int;
        let result = unsafe {
            libc::bind(
                fd,
                (&address as *const libc::sockaddr_ll).cast(),
                mem::size_of::<libc::sockaddr_ll>() as libc::socklen_t,
            )
        };
        if result < 0 {
            let error = io::Error::last_os_error();
            unsafe { libc::close(fd) };
            return Err(error);
        }
        Ok(Self(fd))
    }

    fn receive(&self, buffer: &mut [u8]) -> io::Result<Option<usize>> {
        let mut poll_fd = libc::pollfd {
            fd: self.0,
            events: libc::POLLIN,
            revents: 0,
        };
        let ready = unsafe { libc::poll(&mut poll_fd, 1, 500) };
        if ready < 0 {
            return Err(io::Error::last_os_error());
        }
        if ready == 0 {
            return Ok(None);
        }
        let count = unsafe { libc::recv(self.0, buffer.as_mut_ptr().cast(), buffer.len(), 0) };
        if count < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(Some(count as usize))
    }
}

impl Drop for PacketSocket {
    fn drop(&mut self) {
        unsafe { libc::close(self.0) };
    }
}

#[derive(Default)]
struct Stats {
    tx_payloads: u64,
    tx_frames: u64,
    rx_frames: u64,
    rx_unique: u64,
    rx_duplicates: u64,
    rx_audio_missing: u64,
    inject_recoveries: u64,
}

struct PeerSignal {
    smoothed: f64,
    seen_at: Instant,
    logged_at: Option<Instant>,
}

struct State {
    managed_connected: bool,
    forced_channel: Option<u8>,
    switching_channel: Option<u8>,
    last_managed_channel: Option<u8>,
    last_managed_connection: Option<String>,
    offline_started: Option<Instant>,
    current_channel: Option<u8>,
    last_peer_seen: Option<Instant>,
    last_discovery: Option<Vec<u8>>,
    last_rendezvous_echo: Option<Instant>,
    last_audio_activity: Option<Instant>,
    next_wifi_retry: Instant,
    wifi_reconnect_until: Option<Instant>,
    wifi_reconnect_resets: u8,
    scan_suppressed: bool,
    peer_rssi: HashMap<[u8; 6], PeerSignal>,
    recent_frames: RecentFrameCache,
    audio_sequences: HashMap<([u8; 6], [u8; 16]), u32>,
    stats: Stats,
}

impl State {
    fn new() -> Self {
        Self {
            managed_connected: false,
            forced_channel: None,
            switching_channel: None,
            last_managed_channel: None,
            last_managed_connection: None,
            offline_started: None,
            current_channel: None,
            last_peer_seen: None,
            last_discovery: None,
            last_rendezvous_echo: None,
            last_audio_activity: None,
            next_wifi_retry: Instant::now() + WIFI_RETRY_INTERVAL,
            wifi_reconnect_until: None,
            wifi_reconnect_resets: 0,
            scan_suppressed: false,
            peer_rssi: HashMap::new(),
            recent_frames: RecentFrameCache::new(Duration::from_millis(150)),
            audio_sequences: HashMap::new(),
            stats: Stats::default(),
        }
    }

    fn audio_repeats(&self, now: Instant) -> usize {
        select_audio_repeats(
            self.peer_rssi
                .values()
                .filter(|peer| now.duration_since(peer.seen_at) <= RSSI_FRESH)
                .map(|peer| peer.smoothed),
        )
    }
}

struct Radio {
    injector: RadioInjector,
    sequence: u16,
    random: u64,
}

impl Radio {
    fn jitter(&mut self, max_ms: u64) -> Duration {
        self.random ^= self.random << 13;
        self.random ^= self.random >> 7;
        self.random ^= self.random << 17;
        Duration::from_millis(self.random % (max_ms + 1))
    }
}

struct Shared {
    source: [u8; 6],
    interface: String,
    wlan_interface: String,
    offline_channel: u8,
    scan_channels: Vec<u8>,
    control: Arc<UnixDatagram>,
    clients: Mutex<HashSet<PathBuf>>,
    state: Mutex<State>,
    radio: Mutex<Radio>,
    tune: Mutex<()>,
}

impl Shared {
    fn inject(&self, destination: [u8; 6], payload: &[u8], repeats: usize) -> Result<(), String> {
        let mut radio = self.radio.lock().unwrap();
        let mut sent = 0u64;
        let mut recoveries = 0u64;
        for repeat in 0..repeats {
            let frame = build_espnow_frame(
                self.source,
                destination,
                payload,
                radio.sequence,
                RATE_1_MBPS,
            )?;
            radio.sequence = (radio.sequence + 1) & 0x0fff;
            if let Err(error) = radio.injector.send(&frame) {
                log(
                    "WARN",
                    format!("injection failed; reopening pcap handle: {error}"),
                );
                radio.injector = RadioInjector::open(&self.interface)?;
                radio.injector.send(&frame)?;
                recoveries += 1;
            }
            sent += 1;
            if repeat + 1 < repeats {
                let jitter = radio.jitter(2);
                thread::sleep(REPEAT_INTERVAL + jitter);
            }
        }
        let mut state = self.state.lock().unwrap();
        state.stats.tx_payloads += 1;
        state.stats.tx_frames += sent;
        state.stats.inject_recoveries += recoveries;
        Ok(())
    }

    fn refresh_injector(&self) -> Result<(), String> {
        let mut radio = self.radio.lock().unwrap();
        radio.injector = RadioInjector::open(&self.interface)?;
        Ok(())
    }

    fn announce(&self) {
        let payload = self.state.lock().unwrap().last_discovery.clone();
        if let Some(payload) = payload {
            if let Err(error) = self.inject(BROADCAST, &payload, CONTROL_REPEATS) {
                log("WARN", format!("discovery announcement failed: {error}"));
            }
        }
    }

    fn notify_channel(&self, channel: u8, only: Option<&Path>) {
        let clients = if let Some(path) = only {
            vec![path.to_path_buf()]
        } else {
            self.clients.lock().unwrap().iter().cloned().collect()
        };
        let mode = if self.state.lock().unwrap().forced_channel.is_some() {
            MODE_FIXED
        } else {
            MODE_AUTO
        };
        let message = [STATUS, channel, mode];
        let mut stale = Vec::new();
        for client in clients {
            if let Err(error) = self.control.send_to(&message, &client) {
                log(
                    "INFO",
                    format!("removing unavailable client {}: {error}", client.display()),
                );
                stale.push(client);
            }
        }
        if !stale.is_empty() {
            let mut clients = self.clients.lock().unwrap();
            for client in stale {
                clients.remove(&client);
            }
        }
    }

    fn notify_switching(&self, channel: u8) {
        let clients: Vec<_> = self.clients.lock().unwrap().iter().cloned().collect();
        let message = [STATUS, channel, MODE_SWITCHING];
        let mut stale = Vec::new();
        for client in clients {
            if self.control.send_to(&message, &client).is_err() {
                stale.push(client);
            }
        }
        if !stale.is_empty() {
            let mut clients = self.clients.lock().unwrap();
            for client in stale {
                clients.remove(&client);
            }
        }
    }

    fn set_scan_suppression(&self, enabled: bool) -> bool {
        if self.state.lock().unwrap().scan_suppressed == enabled {
            return true;
        }
        let flag = if enabled { "-c1" } else { "-c0" };
        let result = Command::new("/usr/local/bin/nexutil")
            .args(["-I", &self.wlan_interface, flag])
            .output();
        match result {
            Ok(output) if output.status.success() => {
                self.state.lock().unwrap().scan_suppressed = enabled;
                log(
                    "INFO",
                    format!(
                        "managed Wi-Fi background scans {}",
                        if enabled { "suppressed" } else { "restored" }
                    ),
                );
                true
            }
            Ok(output) => {
                log(
                    "WARN",
                    format!(
                        "could not change scan suppression: {}",
                        String::from_utf8_lossy(&output.stderr).trim()
                    ),
                );
                false
            }
            Err(error) => {
                log("WARN", format!("could not run nexutil: {error}"));
                false
            }
        }
    }

    fn set_offline_channel(&self, channel: u8) -> bool {
        let _tune = self.tune.lock().unwrap();
        if interface_channel(&self.interface) == Some(channel) {
            let changed = {
                let mut state = self.state.lock().unwrap();
                state.switching_channel = None;
                state.current_channel.replace(channel) != Some(channel)
            };
            if changed {
                self.notify_channel(channel, None);
            }
            return true;
        }
        let argument = format!("-k{channel}");
        let mut output = Command::new("/usr/local/bin/nexutil")
            .args(["-I", &self.wlan_interface, &argument])
            .output();
        for _ in 0..3 {
            if output.as_ref().is_ok_and(|value| value.status.success()) {
                thread::sleep(Duration::from_millis(80));
                if interface_channel(&self.interface) == Some(channel) {
                    break;
                }
            }
            output = Command::new("/usr/local/bin/nexutil")
                .args(["-I", &self.wlan_interface, &argument])
                .output();
        }
        match output {
            Ok(output)
                if output.status.success()
                    && interface_channel(&self.interface) == Some(channel) =>
            {
                if let Err(error) = self.refresh_injector() {
                    log(
                        "WARN",
                        format!("could not refresh injector after channel change: {error}"),
                    );
                    return false;
                }
                // Nexmon can keep reporting successful raw writes after a
                // channel change while mode 2 has silently stopped emitting.
                // Re-arm mode only after the replacement injection handle is
                // open; opening a handle after -m2 can disable TX again.
                let mode = Command::new("/usr/local/bin/nexutil")
                    .args(["-I", &self.wlan_interface, "-m2"])
                    .output();
                match mode {
                    Ok(mode) if mode.status.success() => {
                        thread::sleep(Duration::from_millis(500));
                    }
                    Ok(mode) => {
                        log(
                            "WARN",
                            format!(
                                "could not re-arm Nexmon mode 2 after channel change: {}",
                                String::from_utf8_lossy(&mode.stderr).trim()
                            ),
                        );
                        return false;
                    }
                    Err(error) => {
                        log("WARN", format!("could not re-arm Nexmon mode 2: {error}"));
                        return false;
                    }
                }
                if interface_channel(&self.interface) != Some(channel) {
                    log(
                        "WARN",
                        format!("Nexmon mode 2 changed radio away from channel {channel}"),
                    );
                    return false;
                }
                let changed = {
                    let mut state = self.state.lock().unwrap();
                    state.switching_channel = None;
                    state.current_channel.replace(channel) != Some(channel)
                };
                if changed {
                    log("INFO", format!("offline ESP-NOW channel={channel}"));
                    self.notify_channel(channel, None);
                }
                true
            }
            Ok(output) => {
                log(
                    "WARN",
                    format!(
                        "could not tune channel {channel}: {}",
                        String::from_utf8_lossy(&output.stderr).trim()
                    ),
                );
                false
            }
            Err(error) => {
                log(
                    "WARN",
                    format!("could not run nexutil for channel {channel}: {error}"),
                );
                false
            }
        }
    }

    fn disconnect_managed_wifi(&self) {
        if associated_channel(&self.wlan_interface).is_none() {
            return;
        }
        if let Some(connection) = managed_connection(&self.wlan_interface) {
            self.state.lock().unwrap().last_managed_connection = Some(connection);
        }
        self.set_scan_suppression(false);
        match Command::new("/usr/bin/nmcli")
            .args(["device", "disconnect", &self.wlan_interface])
            .output()
        {
            Ok(output) if output.status.success() => {
                self.state.lock().unwrap().managed_connected = false;
                log(
                    "INFO",
                    "managed Wi-Fi disconnected for FIXED ESP-NOW channel",
                );
            }
            Ok(output) => log(
                "WARN",
                format!(
                    "could not disconnect managed Wi-Fi: {}",
                    String::from_utf8_lossy(&output.stderr).trim()
                ),
            ),
            Err(error) => log("WARN", format!("could not run nmcli disconnect: {error}")),
        }
    }

    fn reconnect_managed_wifi(&self) {
        let connection = self.state.lock().unwrap().last_managed_connection.clone();
        let result = if let Some(connection) = connection.as_deref() {
            Command::new("/usr/bin/nmcli")
                .args([
                    "connection",
                    "up",
                    "id",
                    connection,
                    "ifname",
                    &self.wlan_interface,
                ])
                .spawn()
        } else {
            Command::new("/usr/bin/nmcli")
                .args(["device", "connect", &self.wlan_interface])
                .spawn()
        };
        match result {
            Ok(_) => log(
                "INFO",
                format!(
                    "requested managed Wi-Fi reconnect for AUTO mode{}",
                    connection
                        .as_deref()
                        .map(|name| format!(" connection={name}"))
                        .unwrap_or_default()
                ),
            ),
            Err(error) => log("WARN", format!("could not run nmcli connect: {error}")),
        }
    }

    fn reset_managed_wifi(&self) {
        let output = Command::new("/usr/bin/nmcli")
            .args(["device", "disconnect", &self.wlan_interface])
            .output();
        if let Ok(output) = output {
            if !output.status.success() {
                log(
                    "WARN",
                    format!(
                        "could not reset managed Wi-Fi: {}",
                        String::from_utf8_lossy(&output.stderr).trim()
                    ),
                );
            }
        }
        self.reconnect_managed_wifi();
    }
}

fn command_output(program: &str, arguments: &[&str]) -> Option<String> {
    let output = Command::new(program).args(arguments).output().ok()?;
    output
        .status
        .success()
        .then(|| String::from_utf8_lossy(&output.stdout).into_owned())
}

fn associated_channel(interface: &str) -> Option<u8> {
    let link = command_output("/usr/sbin/iw", &["dev", interface, "link"])?;
    if !link_is_associated(&link) {
        return None;
    }
    let info = command_output("/usr/sbin/iw", &["dev", interface, "info"])?;
    parse_iw_channel(&info)
}

fn managed_connection(interface: &str) -> Option<String> {
    command_output(
        "/usr/bin/nmcli",
        &["-g", "GENERAL.CONNECTION", "device", "show", interface],
    )
    .map(|value| value.trim().to_owned())
    .filter(|value| !value.is_empty() && value != "--")
}

fn interface_channel(interface: &str) -> Option<u8> {
    command_output("/usr/sbin/iw", &["dev", interface, "info"])
        .and_then(|output| parse_iw_channel(&output))
}

fn disable_power_save(interface: &str) {
    let output = Command::new("/usr/sbin/iw")
        .args(["dev", interface, "set", "power_save", "off"])
        .output();
    if let Ok(output) = output {
        if !output.status.success() {
            log(
                "WARN",
                format!(
                    "could not disable {interface} power save: {}",
                    String::from_utf8_lossy(&output.stderr).trim()
                ),
            );
        }
    }
}

fn control_loop(shared: Arc<Shared>) {
    let mut buffer = [0u8; 4096];
    while RUNNING.load(Ordering::Relaxed) {
        let (size, address) = match shared.control.recv_from(&mut buffer) {
            Ok(value) => value,
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
                ) =>
            {
                continue
            }
            Err(error) => {
                log("WARN", format!("control receive failed: {error}"));
                continue;
            }
        };
        let Some(path) = address.as_pathname() else {
            continue;
        };
        if size == 1 && buffer[0] == REGISTER {
            shared.clients.lock().unwrap().insert(path.to_path_buf());
            log("INFO", format!("registered client {}", path.display()));
            let current_channel = { shared.state.lock().unwrap().current_channel }
                .or_else(|| interface_channel(&shared.interface));
            if let Some(channel) = current_channel {
                shared.notify_channel(channel, Some(path));
            }
            continue;
        }
        if size == 2 && buffer[0] == SET_CHANNEL && (1..=13).contains(&buffer[1]) {
            let channel = buffer[1];
            shared.clients.lock().unwrap().insert(path.to_path_buf());
            let channel_was_current = {
                let mut state = shared.state.lock().unwrap();
                state.switching_channel = Some(channel);
                state.current_channel == Some(channel)
            };
            shared.notify_switching(channel);
            shared.disconnect_managed_wifi();
            shared.state.lock().unwrap().forced_channel = Some(channel);
            shared.set_scan_suppression(true);
            thread::sleep(CHANNEL_SWITCH_SETTLE);
            if shared.set_offline_channel(channel) {
                log("INFO", format!("ESP-NOW channel fixed at {channel}"));
                // A no-op tune does not emit status, but the mode still changed.
                if channel_was_current {
                    shared.notify_channel(channel, None);
                }
                shared.announce();
            }
            continue;
        }
        if size == 1 && buffer[0] == AUTO_CHANNEL {
            shared.clients.lock().unwrap().insert(path.to_path_buf());
            let return_channel = {
                let mut state = shared.state.lock().unwrap();
                state.forced_channel = None;
                state.switching_channel = None;
                // AUTO explicitly grants NetworkManager an immediate scan
                // window. Mark the offline transition as handled so the
                // channel loop cannot replace it with a 60-second delay.
                state.managed_connected = false;
                state.offline_started = Some(Instant::now());
                state.next_wifi_retry = Instant::now() + WIFI_RECONNECT_WINDOW;
                state.wifi_reconnect_until = Some(Instant::now() + WIFI_RECONNECT_WINDOW);
                state.wifi_reconnect_resets = 0;
                state.last_managed_channel
            };
            let channel_was_current =
                { shared.state.lock().unwrap().current_channel == return_channel };
            if let Some(channel) = return_channel {
                shared.set_offline_channel(channel);
            }
            shared.set_scan_suppression(false);
            shared.reconnect_managed_wifi();
            let channel = return_channel
                .or_else(|| interface_channel(&shared.interface))
                .unwrap_or(shared.offline_channel);
            log(
                "INFO",
                format!("ESP-NOW channel mode AUTO, current={channel}"),
            );
            if return_channel.is_none() || channel_was_current {
                shared.notify_channel(channel, None);
            }
            continue;
        }
        if size < 7 || buffer[0] != TRANSMIT {
            continue;
        }
        let destination: [u8; 6] = buffer[1..7].try_into().unwrap();
        let payload = &buffer[7..size];
        if shared.state.lock().unwrap().switching_channel.is_some() {
            continue;
        }
        if payload.len() > MAX_PAYLOAD {
            log(
                "WARN",
                format!(
                    "dropping oversized payload from {}: {}",
                    path.display(),
                    payload.len()
                ),
            );
            continue;
        }
        shared.clients.lock().unwrap().insert(path.to_path_buf());
        let now = Instant::now();
        let repeats = {
            let mut state = shared.state.lock().unwrap();
            if payload.starts_with(b"WD01") {
                state.last_discovery = Some(payload.to_vec());
                // Discovery is already retained for 20 seconds by the app.
                // During speech, one heartbeat copy avoids a seven-frame
                // burst blocking the real-time audio injection queue.
                if state
                    .last_audio_activity
                    .is_some_and(|last| now.duration_since(last) < AUDIO_ACTIVITY_HOLD)
                {
                    1
                } else {
                    CONTROL_REPEATS
                }
            } else {
                state.last_audio_activity = Some(now);
                state.audio_repeats(now)
            }
        };
        if let Err(error) = shared.inject(destination, payload, repeats) {
            log("WARN", format!("payload injection failed: {error}"));
        }
    }
}

fn track_audio_sequence(state: &mut State, source: [u8; 6], payload: &[u8]) {
    if payload.len() < 28 || !payload.starts_with(b"WT01") {
        return;
    }
    let stream: [u8; 16] = payload[8..24].try_into().unwrap();
    let sequence = u32::from_be_bytes(payload[24..28].try_into().unwrap());
    let key = (source, stream);
    if let Some(previous) = state.audio_sequences.get(&key) {
        if sequence > previous + 1 {
            state.stats.rx_audio_missing += (sequence - previous - 1) as u64;
        }
    }
    state
        .audio_sequences
        .entry(key)
        .and_modify(|old| *old = (*old).max(sequence))
        .or_insert(sequence);
    if state.audio_sequences.len() > 64 {
        state.audio_sequences.retain(|entry, _| *entry == key);
    }
}

fn receive_loop(shared: Arc<Shared>) {
    let socket = match PacketSocket::bind(&shared.interface) {
        Ok(socket) => socket,
        Err(error) => {
            log(
                "ERROR",
                format!("could not bind AF_PACKET on {}: {error}", shared.interface),
            );
            RUNNING.store(false, Ordering::SeqCst);
            return;
        }
    };
    let mut buffer = [0u8; 4096];
    while RUNNING.load(Ordering::Relaxed) {
        let size = match socket.receive(&mut buffer) {
            Ok(Some(size)) => size,
            Ok(None) => continue,
            Err(error) => {
                log("WARN", format!("radio receive failed: {error}"));
                continue;
            }
        };
        let frame = &buffer[..size];
        let rssi = parse_radiotap_signal(frame);
        let Some(parsed) = parse_espnow_frame(frame) else {
            continue;
        };
        if parsed.source == shared.source {
            continue;
        }
        let now = Instant::now();
        let mut rendezvous_echo = false;
        {
            let mut state = shared.state.lock().unwrap();
            state.stats.rx_frames += 1;
            if let Some(rssi) = rssi {
                let peer = state.peer_rssi.entry(parsed.source).or_insert(PeerSignal {
                    smoothed: rssi as f64,
                    seen_at: now,
                    logged_at: None,
                });
                peer.smoothed = peer.smoothed * 0.75 + rssi as f64 * 0.25;
                peer.seen_at = now;
                if peer
                    .logged_at
                    .is_none_or(|logged| now.duration_since(logged) >= STATS_INTERVAL)
                {
                    log(
                        "INFO",
                        format!(
                            "peer={} rssi={:.1} dBm",
                            format_mac(&parsed.source),
                            peer.smoothed
                        ),
                    );
                    peer.logged_at = Some(now);
                }
            }
            if state
                .recent_frames
                .is_duplicate(parsed.source, &parsed.payload, now)
            {
                state.stats.rx_duplicates += 1;
                continue;
            }
            state.stats.rx_unique += 1;
            track_audio_sequence(&mut state, parsed.source, &parsed.payload);
            if parsed.payload.starts_with(b"WD01") {
                state.last_peer_seen = Some(now);
                if !state.managed_connected
                    && state.current_channel != Some(shared.offline_channel)
                    && state.last_discovery.is_some()
                    && state
                        .last_rendezvous_echo
                        .is_none_or(|last| now.duration_since(last) >= Duration::from_secs(1))
                {
                    state.last_rendezvous_echo = Some(now);
                    rendezvous_echo = true;
                }
            } else {
                state.last_audio_activity = Some(now);
            }
        }
        if rendezvous_echo {
            shared.announce();
        }
        let mut message = Vec::with_capacity(7 + parsed.payload.len());
        message.push(FRAME);
        message.extend_from_slice(&parsed.source);
        message.extend_from_slice(&parsed.payload);
        let clients: Vec<_> = shared.clients.lock().unwrap().iter().cloned().collect();
        let mut stale = Vec::new();
        for client in clients {
            if shared.control.send_to(&message, &client).is_err() {
                stale.push(client);
            }
        }
        if !stale.is_empty() {
            let mut clients = shared.clients.lock().unwrap();
            for client in stale {
                clients.remove(&client);
            }
        }
    }
}

fn channel_loop(shared: Arc<Shared>) {
    while RUNNING.load(Ordering::Relaxed) {
        let now = Instant::now();
        if shared.state.lock().unwrap().switching_channel.is_some() {
            thread::sleep(Duration::from_millis(100));
            continue;
        }
        let forced_channel = { shared.state.lock().unwrap().forced_channel };
        if let Some(channel) = forced_channel {
            shared.set_scan_suppression(true);
            if shared.set_offline_channel(channel) {
                // A channel switch can make both peers deaf during the one
                // control announcement sent by SET_CHANNEL.  Keep advertising
                // on the forced channel so they rendezvous within one second.
                shared.announce();
            }
            thread::sleep(FIXED_ANNOUNCE_INTERVAL);
            continue;
        }
        if let Some(channel) = associated_channel(&shared.wlan_interface) {
            // wlan0 and mon0 share one PHY. Background roaming scans briefly
            // take it off-channel and can silently stall BCM43430 injection.
            shared.set_scan_suppression(true);
            let changed = {
                let mut state = shared.state.lock().unwrap();
                let changed = !state.managed_connected || state.current_channel != Some(channel);
                state.managed_connected = true;
                state.last_managed_channel = Some(channel);
                state.last_managed_connection = managed_connection(&shared.wlan_interface)
                    .or_else(|| state.last_managed_connection.clone());
                state.offline_started = None;
                state.wifi_reconnect_until = None;
                state.wifi_reconnect_resets = 0;
                state.current_channel = Some(channel);
                changed
            };
            if changed {
                log(
                    "INFO",
                    format!("managed Wi-Fi associated; ESP-NOW follows channel={channel}"),
                );
                shared.notify_channel(channel, None);
            }
            thread::sleep(Duration::from_secs(1));
            continue;
        }

        // AUTO recovery owns the shared PHY while NetworkManager associates.
        // Retuning mon0 to an offline rendezvous channel during the
        // "configuring" phase can strand wlan0 until the next reboot.
        let reconnect_action = {
            let mut state = shared.state.lock().unwrap();
            match state.wifi_reconnect_until {
                Some(until) if now < until => 1,
                Some(_) if state.wifi_reconnect_resets == 0 => {
                    state.wifi_reconnect_resets = 1;
                    state.wifi_reconnect_until = Some(now + WIFI_RECONNECT_WINDOW);
                    2
                }
                Some(_) => {
                    state.wifi_reconnect_until = None;
                    0
                }
                None => 0,
            }
        };
        if reconnect_action != 0 {
            shared.set_scan_suppression(false);
            if reconnect_action == 2 {
                log(
                    "WARN",
                    "managed Wi-Fi reconnect timed out; resetting activation once",
                );
                shared.reset_managed_wifi();
            }
            thread::sleep(Duration::from_secs(1));
            continue;
        }

        let entering_offline = {
            let mut state = shared.state.lock().unwrap();
            if state.managed_connected || state.offline_started.is_none() {
                state.managed_connected = false;
                state.offline_started = Some(now);
                state.next_wifi_retry = now + WIFI_RETRY_INTERVAL;
                true
            } else {
                false
            }
        };
        if entering_offline {
            log(
                "INFO",
                format!(
                    "managed Wi-Fi unassociated; ESP-NOW falling back to channel={}",
                    shared.offline_channel
                ),
            );
            shared.set_scan_suppression(true);
            shared.set_offline_channel(shared.offline_channel);
            shared.announce();
        }

        let retry_wifi = {
            let state = shared.state.lock().unwrap();
            now >= state.next_wifi_retry
                && state
                    .last_audio_activity
                    .is_none_or(|last| now.duration_since(last) >= AUDIO_ACTIVITY_HOLD)
        };
        if retry_wifi {
            shared.set_scan_suppression(false);
            shared.reconnect_managed_wifi();
            thread::sleep(WIFI_RETRY_WINDOW);
            if associated_channel(&shared.wlan_interface).is_some() {
                continue;
            }
            shared.set_scan_suppression(true);
            shared.set_offline_channel(shared.offline_channel);
            shared.announce();
            shared.state.lock().unwrap().next_wifi_retry = Instant::now() + WIFI_RETRY_INTERVAL;
        }

        let should_scan = {
            let state = shared.state.lock().unwrap();
            let peer_recent = state
                .last_peer_seen
                .is_some_and(|seen| now.duration_since(seen) < OFFLINE_PEER_HOLD);
            let in_grace = state
                .offline_started
                .is_some_and(|start| now.duration_since(start) < OFFLINE_GRACE);
            !peer_recent && !in_grace
        };
        if !should_scan {
            shared.set_offline_channel(shared.offline_channel);
            thread::sleep(Duration::from_secs(1));
            continue;
        }

        let mut choices = shared.scan_channels.clone();
        if !choices.contains(&shared.offline_channel) {
            choices.push(shared.offline_channel);
        }
        // Use a common wall-clock slot so two disconnected devices scan the
        // same channel at the same time. PiSugar's RTC keeps this usable even
        // when neither device can reach NTP.
        let epoch = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default();
        let slot_millis = OFFLINE_SCAN_SLOT.as_millis() as u64;
        let channel = choices[((epoch.as_millis() as u64 / slot_millis) as usize) % choices.len()];
        let scan_started = Instant::now();
        if shared.set_offline_channel(channel) {
            shared.announce();
        }
        let remainder = epoch.as_millis() as u64 % slot_millis;
        let dwell = Duration::from_millis((slot_millis - remainder).max(200));
        while RUNNING.load(Ordering::Relaxed) && scan_started.elapsed() < dwell {
            let found = shared
                .state
                .lock()
                .unwrap()
                .last_peer_seen
                .is_some_and(|seen| seen >= scan_started);
            if found {
                thread::sleep(Duration::from_millis(350));
                shared.set_offline_channel(shared.offline_channel);
                shared.announce();
                shared.state.lock().unwrap().offline_started = Some(Instant::now());
                break;
            }
            thread::sleep(Duration::from_millis(100));
        }
    }
}

fn health_loop(shared: Arc<Shared>) {
    while RUNNING.load(Ordering::Relaxed) {
        thread::sleep(INJECTOR_REFRESH);
        if !RUNNING.load(Ordering::Relaxed) {
            break;
        }
        disable_power_save(&shared.wlan_interface);
        if let Err(error) = shared.refresh_injector() {
            log(
                "WARN",
                format!("could not refresh pcap injection handle: {error}"),
            );
        }
    }
}

fn stats_loop(shared: Arc<Shared>) {
    while RUNNING.load(Ordering::Relaxed) {
        thread::sleep(STATS_INTERVAL);
        let now = Instant::now();
        let mut state = shared.state.lock().unwrap();
        let peers = state
            .peer_rssi
            .iter()
            .filter(|(_, peer)| now.duration_since(peer.seen_at) <= RSSI_FRESH)
            .map(|(mac, peer)| format!("{}:{:.1}", format_mac(mac), peer.smoothed))
            .collect::<Vec<_>>();
        let repeats = state.audio_repeats(now);
        let average = if state.stats.tx_payloads == 0 {
            0.0
        } else {
            state.stats.tx_frames as f64 / state.stats.tx_payloads as f64
        };
        log(
            "INFO",
            format!(
                "link-stats tx_payloads={} tx_frames={} avg_copies={average:.1} audio_copies={repeats} rx_frames={} rx_unique={} rx_duplicates={} rx_audio_missing={} inject_recoveries={} peers={}",
                state.stats.tx_payloads,
                state.stats.tx_frames,
                state.stats.rx_frames,
                state.stats.rx_unique,
                state.stats.rx_duplicates,
                state.stats.rx_audio_missing,
                state.stats.inject_recoveries,
                if peers.is_empty() { "none".to_string() } else { peers.join(",") },
            ),
        );
        state.stats = Stats::default();
    }
}

struct Args {
    interface: String,
    wlan_interface: String,
    socket: PathBuf,
    offline_channel: u8,
    scan_channels: Vec<u8>,
}

fn parse_args() -> Result<Args, String> {
    let mut result = Args {
        interface: "mon0".into(),
        wlan_interface: "wlan0".into(),
        socket: "/run/whisplay-espnow/bridge.sock".into(),
        offline_channel: 6,
        scan_channels: vec![1, 6, 11],
    };
    let mut arguments = std::env::args().skip(1);
    while let Some(argument) = arguments.next() {
        let value = |arguments: &mut std::iter::Skip<std::env::Args>, name: &str| {
            arguments
                .next()
                .ok_or_else(|| format!("missing value for {name}"))
        };
        match argument.as_str() {
            "--interface" => result.interface = value(&mut arguments, "--interface")?,
            "--wlan-interface" => {
                result.wlan_interface = value(&mut arguments, "--wlan-interface")?
            }
            "--socket" => result.socket = value(&mut arguments, "--socket")?.into(),
            "--offline-channel" => {
                result.offline_channel = value(&mut arguments, "--offline-channel")?
                    .parse()
                    .map_err(|_| "invalid offline channel")?
            }
            "--scan-channels" => {
                let channels = value(&mut arguments, "--scan-channels")?;
                result.scan_channels = channels
                    .split(',')
                    .map(|item| {
                        item.parse::<u8>()
                            .map_err(|_| "invalid scan channel".to_string())
                    })
                    .collect::<Result<_, _>>()?;
            }
            "-h" | "--help" => {
                println!("whisplay-espnow-bridge [--interface mon0] [--wlan-interface wlan0] [--socket PATH] [--offline-channel 6] [--scan-channels 1,6,11]");
                std::process::exit(0);
            }
            _ => return Err(format!("unknown argument: {argument}")),
        }
    }
    if !(1..=13).contains(&result.offline_channel)
        || result
            .scan_channels
            .iter()
            .any(|channel| !(1..=13).contains(channel))
    {
        return Err("channels must be between 1 and 13".into());
    }
    Ok(result)
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = parse_args().map_err(io::Error::other)?;
    unsafe {
        libc::signal(libc::SIGINT, stop_signal as libc::sighandler_t);
        libc::signal(libc::SIGTERM, stop_signal as libc::sighandler_t);
    }
    let source = parse_mac(&fs::read_to_string(format!(
        "/sys/class/net/{}/address",
        args.wlan_interface
    ))?)
    .map_err(io::Error::other)?;
    if let Some(parent) = args.socket.parent() {
        fs::create_dir_all(parent)?;
    }
    if args.socket.exists() {
        fs::remove_file(&args.socket)?;
    }
    let control = Arc::new(UnixDatagram::bind(&args.socket)?);
    control.set_read_timeout(Some(Duration::from_millis(500)))?;
    fs::set_permissions(&args.socket, fs::Permissions::from_mode(0o666))?;
    let injector = RadioInjector::open(&args.interface).map_err(io::Error::other)?;
    let shared = Arc::new(Shared {
        source,
        interface: args.interface,
        wlan_interface: args.wlan_interface,
        offline_channel: args.offline_channel,
        scan_channels: args.scan_channels,
        control,
        clients: Mutex::new(HashSet::new()),
        state: Mutex::new(State::new()),
        radio: Mutex::new(Radio {
            injector,
            sequence: 0,
            random: u64::from_be_bytes([
                0, 0, source[0], source[1], source[2], source[3], source[4], source[5],
            ]),
        }),
        tune: Mutex::new(()),
    });
    disable_power_save(&shared.wlan_interface);
    log(
        "INFO",
        format!(
            "ready interface={} source={} socket={}",
            shared.interface,
            format_mac(&source),
            args.socket.display()
        ),
    );

    let _threads = [
        {
            let shared = shared.clone();
            thread::spawn(move || control_loop(shared))
        },
        {
            let shared = shared.clone();
            thread::spawn(move || receive_loop(shared))
        },
        {
            let shared = shared.clone();
            thread::spawn(move || channel_loop(shared))
        },
        {
            let shared = shared.clone();
            thread::spawn(move || health_loop(shared))
        },
        {
            let shared = shared.clone();
            thread::spawn(move || stats_loop(shared))
        },
    ];
    while RUNNING.load(Ordering::Relaxed) {
        thread::sleep(Duration::from_millis(200));
    }
    let was_forced = shared.state.lock().unwrap().forced_channel.is_some();
    if shared.state.lock().unwrap().scan_suppressed {
        shared.set_scan_suppression(false);
    }
    if was_forced {
        shared.reconnect_managed_wifi();
    }
    // Do not join radio worker threads during shutdown.  A libpcap operation can
    // remain blocked while nexmon-monitor is rebuilding mon0, which otherwise
    // leaves systemd stuck in "deactivating" until its stop timeout expires.
    // Returning from main terminates the process and lets the OS close the raw
    // sockets after the small amount of synchronous cleanup above.
    let _ = fs::remove_file(&args.socket);
    Ok(())
}
