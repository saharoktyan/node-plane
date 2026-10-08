use crate::{progress::Progress, ssh::Interaction};
use anyhow::{Result, bail};
use std::sync::{Arc, mpsc};
use zeroize::Zeroizing;

pub enum Prompt {
    HostKey { host: String, fingerprint: String },
    Password { user: String, host: String },
    ConfirmAction { title: String, description: String },
}
pub enum Answer {
    Confirm(bool),
    Password(Zeroizing<String>),
    Cancel,
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
impl Interaction for UiInteraction {
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
