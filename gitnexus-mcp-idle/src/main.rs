//! Run a stdio MCP server only while it is in use.
//!
//! Usage: gitnexus-mcp-idle <idle-seconds> <command> [args...]
//!
//! Codex keeps each stdio MCP server of a thread running until the thread ends.
//! This launcher forwards every JSON-RPC line between the client and the server
//! unchanged. After <idle-seconds> without traffic and with no request awaiting
//! its reply, it closes the server's stdin so the server exits. The next request
//! starts the server again; the launcher first replays the client's initialize
//! handshake to it. If the server exits on its own, the launcher exits with the
//! server's status, as the server would have.

use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader, Read, Write};
use std::process::{exit, Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{channel, RecvTimeoutError, Sender};
use std::time::{Duration, Instant};

const REPLAY_ID: &str = "gitnexus-mcp-idle:initialize";

enum Event {
    Client(Option<Vec<u8>>),
    Server(u64, Option<Vec<u8>>),
}

struct Server {
    generation: u64,
    child: Child,
    stdin: ChildStdin,
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let idle = match args
        .first()
        .and_then(|secs| secs.parse::<f64>().ok())
        .and_then(|secs| Duration::try_from_secs_f64(secs).ok())
    {
        Some(idle) if args.len() > 1 => idle,
        _ => {
            eprintln!("usage: gitnexus-mcp-idle <idle-seconds> <command> [args...]");
            exit(2);
        }
    };
    let command = &args[1..];
    let (events, inbox) = channel();
    forward_lines(std::io::stdin(), events.clone(), Event::Client);

    let mut out = std::io::stdout().lock();
    let mut server: Option<Server> = None;
    let mut generation = 0;
    let mut pending: HashSet<String> = HashSet::new();
    let mut replaying: HashMap<u64, Vec<u8>> = HashMap::new();
    let mut handshake: Vec<u8> = Vec::new();
    let mut last = Instant::now();
    loop {
        let event = if server.is_some() && pending.is_empty() {
            match inbox.recv_timeout(idle.saturating_sub(last.elapsed())) {
                Err(RecvTimeoutError::Timeout) => {
                    // Close stdin and finish this worker before starting its successor.
                    let Server {
                        mut child, stdin, ..
                    } = server.take().expect("server is running");
                    drop(stdin);
                    child.wait().ok();
                    continue;
                }
                event => event.expect("main holds a sender"),
            }
        } else {
            inbox.recv().expect("main holds a sender")
        };
        last = Instant::now();
        match event {
            Event::Client(Some(line)) => {
                let message: Value = serde_json::from_slice(&line).unwrap_or_default();
                let method = message.get("method").and_then(Value::as_str);
                let request = method.is_some() && message.get("id").is_some();
                match method {
                    Some("initialize") => {
                        let mut replay = message.clone();
                        replay["id"] = REPLAY_ID.into();
                        handshake = serde_json::to_vec(&replay).expect("a JSON value serializes");
                        handshake.push(b'\n');
                    }
                    Some("notifications/initialized") => handshake.extend_from_slice(&line),
                    Some("notifications/cancelled") => {
                        pending.remove(&message["params"]["requestId"].to_string());
                        if message["params"]["requestId"] == REPLAY_ID {
                            if let Some(queued) = replaying.get_mut(&generation) {
                                queued.clear();
                                continue;
                            }
                        }
                    }
                    _ => {}
                }
                if request {
                    pending.insert(message["id"].to_string());
                }
                let running = match server.as_mut() {
                    Some(running) => running,
                    None if !request => continue,
                    None => {
                        generation += 1;
                        let started = server.insert(start(command, generation, &events));
                        if method != Some("initialize") {
                            replaying.insert(generation, Vec::new());
                            // A failed write means the server exited; its stdout end then ends the launcher.
                            let _ = started.stdin.write_all(&handshake);
                        }
                        started
                    }
                };
                if request && message["id"] == REPLAY_ID {
                    if let Some(queued) = replaying.get_mut(&running.generation) {
                        queued.extend_from_slice(&line);
                        continue;
                    }
                }
                let _ = running.stdin.write_all(&line);
            }
            Event::Server(sent, Some(line)) => {
                let message: Value = serde_json::from_slice(&line).unwrap_or_default();
                if message.get("method").is_none() {
                    if message["id"] == REPLAY_ID {
                        if let Some(queued) = replaying.remove(&sent) {
                            if !queued.is_empty() {
                                let running = server
                                    .as_mut()
                                    .expect("queued request has an active worker");
                                let _ = running.stdin.write_all(&queued);
                            }
                            continue;
                        }
                    }
                    pending.remove(&message["id"].to_string());
                }
                if out.write_all(&line).and_then(|()| out.flush()).is_err() {
                    finish(server);
                }
            }
            Event::Server(ended, None) => {
                replaying.remove(&ended);
                if server
                    .as_ref()
                    .is_some_and(|running| running.generation == ended)
                {
                    finish(server);
                }
            }
            Event::Client(None) => finish(server),
        }
    }
}

/// Start the server and forward its stdout lines as events of this generation.
fn start(command: &[String], generation: u64, events: &Sender<Event>) -> Server {
    let mut child = Command::new(&command[0])
        .args(&command[1..])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap_or_else(|error| {
            eprintln!("gitnexus-mcp-idle: cannot start {}: {error}", command[0]);
            exit(127);
        });
    forward_lines(
        child.stdout.take().expect("stdout is piped"),
        events.clone(),
        move |line| Event::Server(generation, line),
    );
    Server {
        generation,
        stdin: child.stdin.take().expect("stdin is piped"),
        child,
    }
}

/// Send each line of `input` as an event, then `None` at its end.
fn forward_lines<R: Read + Send + 'static>(
    input: R,
    events: Sender<Event>,
    event: impl Fn(Option<Vec<u8>>) -> Event + Send + 'static,
) {
    std::thread::spawn(move || {
        let mut reader = BufReader::new(input);
        loop {
            let mut line = Vec::new();
            let more = matches!(reader.read_until(b'\n', &mut line), Ok(n) if n > 0);
            if events.send(event(more.then_some(line))).is_err() || !more {
                return;
            }
        }
    });
}

/// The client is gone: close the server's stdin and exit with its status.
fn finish(server: Option<Server>) -> ! {
    let code = server.map_or(
        Some(0),
        |Server {
             mut child, stdin, ..
         }| {
            drop(stdin);
            child.wait().ok().and_then(|status| status.code())
        },
    );
    exit(code.unwrap_or(1));
}
