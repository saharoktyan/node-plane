mod audit;
mod backend;
mod config;
mod connections;
mod enrollment;
mod events;
mod installer;
mod operation_store;
mod progress;
mod self_manage;
mod ssh;
mod tui;
mod workstation;

use anyhow::{Result, ensure};
use clap::{Args, Parser, Subcommand};
use config::{Action, Request, WorkflowOptions};
use events::{Answer, Event, Prompt, UiInteraction};
use std::{
    io::{self, IsTerminal, Write},
    path::PathBuf,
    sync::{Arc, mpsc},
};
use zeroize::Zeroizing;

#[derive(Parser)]
#[command(
    name = "node-plane",
    version,
    about = "Install, update and diagnose Node Plane from your workstation"
)]
struct Cli {
    /// Use concise terminal output instead of the full-screen interface.
    #[arg(long, global = true)]
    no_tui: bool,
    /// Private local SSH key, host trust and redacted operation logs.
    #[arg(long, global = true)]
    state_dir: Option<PathBuf>,
    #[command(subcommand)]
    command: Option<Command>,
}
#[derive(Args)]
struct Connection {
    /// VPS hostname or IP address (without root@ or ssh://).
    host: String,
    #[arg(long, default_value = "root")]
    user: String,
    #[arg(long, default_value_t = 22)]
    port: u16,
}
#[derive(Subcommand)]
enum Command {
    /// Manage this workstation binary locally; never modifies a VPS.
    #[command(name = "self")]
    SelfManage {
        #[command(subcommand)]
        command: self_manage::Command,
    },
    /// Install the systemd backend, worker, Telegram client and driver.
    Install {
        #[command(flatten)]
        connection: Connection,
        #[arg(long, default_value = "dev")]
        branch: String,
        /// Exact release version; omit to install the latest release in the channel.
        #[arg(long, default_value = "")]
        tag: String,
        /// Numeric Telegram administrator IDs, separated by commas.
        #[arg(long, default_value = "")]
        admins: String,
    },
    /// Read-only installation checks (first SSH connection can enroll the workstation key).
    Diagnose {
        #[command(flatten)]
        connection: Connection,
        /// Offer confirmed recovery of stopped controller services after diagnosis.
        #[arg(long)]
        repair: bool,
    },
    /// Update the whole installed stack, or resume observing an existing update.
    Update {
        #[command(flatten)]
        connection: Connection,
        #[arg(long, default_value = "")]
        account: String,
        #[arg(long, default_value = "")]
        branch: String,
        #[arg(long, default_value = "")]
        tag: String,
        #[arg(long)]
        /// Saved local operation UUID printed by the resume instruction.
        operation: Option<uuid::Uuid>,
        /// Accept the update after discovery. Does not bypass SSH host trust.
        #[arg(long)]
        yes: bool,
    },
    /// Add the controller's public key to a target VPS and verify its SSH access.
    PrepareNode {
        #[command(flatten)]
        connection: Connection,
        #[arg(long)]
        target: String,
        #[arg(long, default_value = "root")]
        target_user: String,
        #[arg(long, default_value_t = 22)]
        target_port: u16,
        #[arg(long, default_value = "")]
        account: String,
        #[arg(long)]
        yes: bool,
    },
}

fn main() {
    if let Err(error) = entry() {
        eprintln!("{error:#}");
        std::process::exit(1);
    }
}
fn entry() -> Result<()> {
    let cli = Cli::parse();
    if let Some(Command::SelfManage { command }) = &cli.command {
        return self_manage::execute(command);
    }
    let state = cli.state_dir.unwrap_or(
        dirs::data_local_dir()
            .ok_or_else(|| {
                anyhow::anyhow!(
                    "Cannot locate your private application data directory. Use --state-dir."
                )
            })?
            .join("node-plane"),
    );
    let mut request = Request {
        action: Action::Install,
        host: String::new(),
        port: 22,
        user: "root".into(),
        state_dir: state,
        tag: String::new(),
        branch: "dev".into(),
        admin_ids: String::new(),
        bot_token: Zeroizing::new(String::new()),
        workflow: WorkflowOptions::default(),
    };
    match cli.command {
        Some(Command::Install {
            connection,
            branch,
            tag,
            admins,
        }) => {
            request.host = connection.host;
            request.port = connection.port;
            request.user = connection.user;
            request.branch = branch;
            request.tag = tag;
            request.admin_ids = admins;
        }
        Some(Command::Diagnose { connection, repair }) => {
            request.workflow.repair = repair;
            request.action = Action::Diagnose;
            request.host = connection.host;
            request.port = connection.port;
            request.user = connection.user;
        }
        Some(Command::Update {
            connection,
            account,
            branch,
            tag,
            operation,
            yes,
        }) => {
            request.action = Action::Update;
            request.host = connection.host;
            request.port = connection.port;
            request.user = connection.user;
            request.branch = branch;
            request.tag = tag;
            request.workflow.account = account;
            request.workflow.operation = operation;
            request.workflow.yes = yes;
        }
        Some(Command::PrepareNode {
            connection,
            target,
            target_user,
            target_port,
            account,
            yes,
        }) => {
            request.action = Action::PrepareNode;
            request.host = connection.host;
            request.port = connection.port;
            request.user = connection.user;
            request.branch.clear();
            request.workflow.target_host = target;
            request.workflow.target_user = target_user;
            request.workflow.target_port = target_port;
            request.workflow.account = account;
            request.workflow.yes = yes;
        }
        None => {}
        Some(Command::SelfManage { .. }) => unreachable!(),
    }
    if !cli.no_tui && io::stdin().is_terminal() && io::stdout().is_terminal() {
        return tui::run(request);
    }
    ensure!(
        !request.host.is_empty(),
        "Specify a command and controller hostname when the full-screen interface is disabled."
    );
    if request.action == Action::Install {
        if request.admin_ids.is_empty() {
            request.admin_ids = read_line("Telegram administrator ID(s): ")?;
        }
        request.bot_token = match std::env::var("NODE_PLANE_BOT_TOKEN") {
            Ok(value) => Zeroizing::new(value),
            Err(_) => {
                ensure!(
                    io::stdin().is_terminal(),
                    "Set NODE_PLANE_BOT_TOKEN for noninteractive installation; never pass a token in command arguments."
                );
                Zeroizing::new(rpassword::prompt_password("BotFather token (hidden): ")?)
            }
        };
    }
    request.validate()?;
    if request.action == Action::Install && io::stdin().is_terminal() {
        ensure!(
            read_line(&format!(
                "Install the systemd stack on {}? [y/N]: ",
                request.host
            ))?
            .eq_ignore_ascii_case("y"),
            "Installation cancelled."
        );
    }
    let (tx, rx) = mpsc::channel();
    let interaction: Arc<dyn ssh::Interaction> = UiInteraction::shared(tx.clone());
    let worker = spawn(request, interaction, tx);
    let mut result = None;
    let mut last_stage = String::new();
    for event in rx {
        match event {
            Event::Stage(label) => {
                if last_stage != label {
                    println!("{label}...");
                    last_stage = label;
                }
            }
            Event::Update(snapshot) => {
                println!(
                    "Update {}: {} ({})",
                    snapshot.id, snapshot.status, snapshot.phase
                );
                for (component, status) in snapshot.components {
                    println!("  {component}: {status}");
                }
                for node in snapshot.nodes {
                    println!(
                        "  {} · {}: {} {}",
                        node.region,
                        if node.title.is_empty() {
                            &node.key
                        } else {
                            &node.title
                        },
                        node.status_label(),
                        node.error
                    );
                }
            }
            Event::Progress(p) => {
                if p.event == "step" {
                    println!(
                        "{}/{} complete | {}: {}",
                        p.completed, p.total, p.label, p.state
                    );
                }
            }
            Event::Prompt(prompt, reply) => {
                let answer = console_prompt(prompt);
                let _ = reply.send(answer?);
            }
            Event::Finished(outcome) => {
                result = Some(outcome);
                break;
            }
        }
    }
    worker.join().map_err(|_| {
        anyhow::anyhow!("Operation worker failed. Diagnose the server before retrying.")
    })?;
    match result {
        Some(Ok(message)) => {
            println!("{message}");
            Ok(())
        }
        Some(Err(error)) => Err(anyhow::anyhow!(error)),
        None => Err(anyhow::anyhow!(
            "Operation ended without a result. Diagnose the server before retrying."
        )),
    }
}
fn read_line(prompt: &str) -> Result<String> {
    ensure!(
        io::stdin().is_terminal(),
        "Interactive input is required. Supply all nonsecret command options first."
    );
    print!("{prompt}");
    io::stdout().flush()?;
    let mut line = String::new();
    io::stdin().read_line(&mut line)?;
    Ok(line.trim().into())
}
fn console_prompt(prompt: Prompt) -> Result<Answer> {
    match prompt {
        Prompt::HostKey { host, fingerprint } => {
            ensure!(
                io::stdin().is_terminal(),
                "Unknown SSH host {host} ({fingerprint}). Run interactively to confirm its fingerprint before authentication."
            );
            println!(
                "First connection to {host}\nSSH host fingerprint: {fingerprint}\nCompare it with the VPS provider or another trusted source."
            );
            Ok(Answer::Confirm(
                read_line("Trust this host? [y/N]: ")?.eq_ignore_ascii_case("y"),
            ))
        }
        Prompt::Password { user, host } => {
            ensure!(
                io::stdin().is_terminal(),
                "The workstation key is not authorized on {host}. Run interactively once to enter the SSH password and enroll the public key."
            );
            Ok(Answer::Password(Zeroizing::new(
                rpassword::prompt_password(format!("SSH password for {user}@{host} (hidden): "))?,
            )))
        }
        Prompt::ConfirmAction { title, description } => {
            println!("{title}\n{description}");
            Ok(Answer::Confirm(
                read_line("Continue? [y/N]: ")?.eq_ignore_ascii_case("y"),
            ))
        }
    }
}
pub fn spawn(
    request: Request,
    interaction: Arc<dyn ssh::Interaction>,
    tx: mpsc::Sender<Event>,
) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        let result = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .map_err(anyhow::Error::from)
            .and_then(|runtime| {
                runtime.block_on(async {
                    match request.action {
                        Action::Install | Action::Diagnose => {
                            installer::execute(request, interaction, tx.clone()).await
                        }
                        Action::Update | Action::PrepareNode => {
                            workstation::execute(request, interaction, tx.clone()).await
                        }
                    }
                })
            });
        let _ = tx.send(Event::Finished(result.map_err(|e| format!("{e:#}"))));
    })
}
