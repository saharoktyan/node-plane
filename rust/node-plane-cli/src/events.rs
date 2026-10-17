use crate::{progress::Progress, ssh::Interaction};
use anyhow::{Result, bail};
use std::sync::{Arc, mpsc};
use zeroize::Zeroizing;

pub enum Prompt {
    HostKey {
        host: String,
        fingerprint: String,
    },
    Select {
        title: String,
        description: String,
        choices: Vec<(String, String)>,
    },
    Text {
        title: String,
        description: String,
        secret: bool,
    },
    Password {
        user: String,
        host: String,
    },
    ConfirmAction {
        title: String,
        description: String,
    },
    SelectAdministrator {
        choices: Vec<(String, String)>,
    },
}
pub enum Answer {
    Confirm(bool),
    Password(Zeroizing<String>),
    Cancel,
    Selection(usize),
}
pub enum Event {
    Stage(String),
    Progress(Progress),
    Update(crate::workstation::UpdateSnapshot),
    Nodes(crate::nodes::Update),
    Prompt(Prompt, mpsc::Sender<Answer>),
    Finished(Result<String, String>),
}

pub struct UiInteraction(pub mpsc::Sender<Event>);
impl UiInteraction {
    fn ask(&self, prompt: Prompt) -> Result<Answer> {
        let (tx, rx) = mpsc::channel();
        self.0.send(Event::Prompt(prompt, tx))?;
        Ok(rx.recv()?)
    }
    pub fn shared(tx: mpsc::Sender<Event>) -> Arc<dyn Interaction> {
        Arc::new(Self(tx))
    }
}
pub fn confirm(tx: &mpsc::Sender<Event>, title: &str, description: String) -> Result<bool> {
    match UiInteraction(tx.clone()).ask(Prompt::ConfirmAction {
        title: title.into(),
        description,
    })? {
        Answer::Confirm(value) => Ok(value),
        _ => Ok(false),
    }
}
pub fn select_administrator(
    tx: &mpsc::Sender<Event>,
    choices: Vec<(String, String)>,
) -> Result<usize> {
    match UiInteraction(tx.clone()).ask(Prompt::SelectAdministrator { choices })? {
        Answer::Selection(index) => Ok(index),
        _ => bail!("Workstation registration cancelled"),
    }
}
impl Interaction for UiInteraction {
    fn authentication(&self, user: &str, host: &str) -> Result<crate::ssh::Authentication> {
        match self.ask(Prompt::Select {
            title: "SSH authentication".into(),
            description: format!("First login: {user}@{host}. Choose an existing credential."),
            choices: vec![
                ("password".into(), "Password / keyboard-interactive".into()),
                ("key".into(), "Existing SSH private key".into()),
            ],
        })? {
            Answer::Selection(0) => Ok(crate::ssh::Authentication::Password),
            Answer::Selection(1) => {
                let path = self.input("SSH private key", "Path to your existing private key (for example ~/.ssh/id_ed25519). The file stays on this computer.", false)?;
                Ok(crate::ssh::Authentication::Key(std::path::PathBuf::from(
                    path.as_str(),
                )))
            }
            _ => bail!("SSH authentication cancelled."),
        }
    }
    fn input(&self, title: &str, description: &str, secret: bool) -> Result<Zeroizing<String>> {
        match self.ask(Prompt::Text {
            title: title.into(),
            description: description.into(),
            secret,
        })? {
            Answer::Password(value) => Ok(value),
            _ => bail!("SSH authentication cancelled."),
        }
    }

    fn confirm_host_key(&self, host: &str, fingerprint: &str) -> Result<bool> {
        match self.ask(Prompt::HostKey {
            host: host.into(),
            fingerprint: fingerprint.into(),
        })? {
            Answer::Confirm(value) => Ok(value),
            _ => bail!("Host verification cancelled."),
        }
    }
    fn password(&self, user: &str, host: &str) -> Result<Zeroizing<String>> {
        match self.ask(Prompt::Password {
            user: user.into(),
            host: host.into(),
        })? {
            Answer::Password(value) => Ok(value),
            _ => bail!("SSH authentication cancelled."),
        }
    }
}
