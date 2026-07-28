use std::error::Error;

use zbus::zvariant::OwnedObjectPath;
use zbus::{Connection, Proxy};

type AnyError = Box<dyn Error + Send + Sync>;

const LOGIN_DESTINATION: &str = "org.freedesktop.login1";
const LOGIN_MANAGER_PATH: &str = "/org/freedesktop/login1";
const LOGIN_MANAGER_INTERFACE: &str = "org.freedesktop.login1.Manager";
const LOGIN_SESSION_INTERFACE: &str = "org.freedesktop.login1.Session";

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SessionIdentity {
    pub id: String,
    pub uid: u32,
    pub seat: String,
    pub session_type: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct SessionRecord {
    identity: SessionIdentity,
    path: OwnedObjectPath,
    active: bool,
    remote: bool,
    class: String,
}

impl SessionRecord {
    fn is_supported_graphical(&self) -> bool {
        self.active
            && !self.remote
            && self.class == "user"
            && !self.identity.seat.is_empty()
            && matches!(self.identity.session_type.as_str(), "wayland" | "x11")
    }
}

#[derive(Clone)]
pub struct SessionAuthorizer {
    connection: Connection,
}

impl SessionAuthorizer {
    pub async fn connect() -> Result<Self, AnyError> {
        Ok(Self {
            connection: Connection::system().await?,
        })
    }

    async fn manager(&self) -> Result<Proxy<'_>, zbus::Error> {
        Proxy::new(
            &self.connection,
            LOGIN_DESTINATION,
            LOGIN_MANAGER_PATH,
            LOGIN_MANAGER_INTERFACE,
        )
        .await
    }

    async fn record(
        &self,
        id: String,
        uid: u32,
        seat: String,
        path: OwnedObjectPath,
    ) -> Result<SessionRecord, AnyError> {
        let session = Proxy::new(
            &self.connection,
            LOGIN_DESTINATION,
            path.as_str(),
            LOGIN_SESSION_INTERFACE,
        )
        .await?;
        let session_type = session.get_property::<String>("Type").await?;
        let active = session.get_property::<bool>("Active").await?;
        let remote = session.get_property::<bool>("Remote").await?;
        let class = session.get_property::<String>("Class").await?;
        drop(session);
        Ok(SessionRecord {
            identity: SessionIdentity {
                id,
                uid,
                seat,
                session_type,
            },
            path,
            active,
            remote,
            class,
        })
    }

    pub async fn authorize(&self, uid: u32) -> Result<SessionIdentity, AnyError> {
        let manager = self.manager().await?;
        let sessions: Vec<(String, u32, String, String, OwnedObjectPath)> =
            manager.call("ListSessions", &()).await?;
        let mut graphical = Vec::new();
        for (id, session_uid, _user, seat, path) in sessions {
            let record = self.record(id, session_uid, seat, path).await?;
            if record.is_supported_graphical() {
                graphical.push(record);
            }
        }
        authorize_records(uid, &graphical)
            .ok_or_else(|| "peer is not the sole active local graphical session".into())
    }
}

fn authorize_records(peer_uid: u32, graphical: &[SessionRecord]) -> Option<SessionIdentity> {
    if graphical.len() != 1 {
        return None;
    }
    let record = &graphical[0];
    (record.identity.uid == peer_uid).then(|| record.identity.clone())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn record(
        path: &str,
        uid: u32,
        active: bool,
        remote: bool,
        class: &str,
        session_type: &str,
        seat: &str,
    ) -> SessionRecord {
        SessionRecord {
            identity: SessionIdentity {
                id: path.rsplit('/').next().unwrap().to_owned(),
                uid,
                seat: seat.to_owned(),
                session_type: session_type.to_owned(),
            },
            path: OwnedObjectPath::try_from(path).unwrap(),
            active,
            remote,
            class: class.to_owned(),
        }
    }

    #[test]
    fn authorizes_only_matching_local_graphical_peer() {
        let sessions = [record(
            "/org/freedesktop/login1/session/_31",
            1000,
            true,
            false,
            "user",
            "wayland",
            "seat0",
        )];
        assert!(authorize_records(1000, &sessions).is_some());
        assert!(authorize_records(1001, &sessions).is_none());
    }

    #[test]
    fn rejects_remote_non_graphical_and_inactive_sessions() {
        for session in [
            record(
                "/org/freedesktop/login1/session/_1",
                1000,
                true,
                true,
                "user",
                "wayland",
                "seat0",
            ),
            record(
                "/org/freedesktop/login1/session/_2",
                1000,
                true,
                false,
                "user",
                "tty",
                "seat0",
            ),
            record(
                "/org/freedesktop/login1/session/_3",
                1000,
                false,
                false,
                "user",
                "x11",
                "seat0",
            ),
            record(
                "/org/freedesktop/login1/session/_4",
                1000,
                true,
                false,
                "greeter",
                "wayland",
                "seat0",
            ),
            record(
                "/org/freedesktop/login1/session/_5",
                1000,
                true,
                false,
                "user",
                "wayland",
                "",
            ),
        ] {
            let graphical: Vec<_> = [session]
                .into_iter()
                .filter(SessionRecord::is_supported_graphical)
                .collect();
            assert!(authorize_records(1000, &graphical).is_none());
        }
    }

    #[test]
    fn rejects_ambiguous_multiple_active_seats() {
        let sessions = [
            record(
                "/org/freedesktop/login1/session/_1",
                1000,
                true,
                false,
                "user",
                "wayland",
                "seat0",
            ),
            record(
                "/org/freedesktop/login1/session/_2",
                1001,
                true,
                false,
                "user",
                "x11",
                "seat1",
            ),
        ];
        assert!(authorize_records(1000, &sessions).is_none());
    }
}
