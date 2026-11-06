//! The web cabinet's files, served beside the API when the node has them.
//!
//! The cabinet is a single-page app built apart from the relay: a directory
//! with `index.html` and its assets. A path that names a file gets the file;
//! any other path gets `index.html`, and the app routes in the browser. The
//! build's hashed assets under `_app/immutable/` never change and are cached
//! for a year; everything else is checked again each time. The page may be
//! framed only by Telegram, for the Mini App; its own scripts are pinned by
//! the content policy the build writes into the page.
use std::path::{Component, Path, PathBuf};

use axum::{
    body::Body,
    http::{HeaderMap, HeaderValue, Method, StatusCode, header},
    response::Response,
};

/// The largest file the cabinet serves; its biggest chunk is far smaller.
const MAX_FILE: u64 = 8 << 20;

/// The file a request path names inside `root`, or `None` when the path is
/// not a plain relative one (`..`, a drive, a NUL).
fn resolve(root: &Path, path: &str) -> Option<PathBuf> {
    let mut out = root.to_path_buf();
    for part in path.trim_start_matches('/').split('/') {
        if part.is_empty() {
            continue;
        }
        let mut parts = Path::new(part).components();
        match (parts.next(), parts.next()) {
            (Some(Component::Normal(name)), None) if !part.contains('\0') => out.push(name),
            _ => return None,
        }
    }
    Some(out)
}

fn content_type(path: &Path) -> &'static str {
    match path.extension().and_then(|e| e.to_str()).unwrap_or("") {
        "html" => "text/html; charset=utf-8",
        "js" | "mjs" => "text/javascript; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "json" => "application/json",
        "webmanifest" => "application/manifest+json",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "ico" => "image/x-icon",
        "woff2" => "font/woff2",
        "woff" => "font/woff",
        "txt" => "text/plain; charset=utf-8",
        _ => "application/octet-stream",
    }
}

fn headers(out: &mut HeaderMap, path: &Path, immutable: bool) {
    let set = |out: &mut HeaderMap, name: header::HeaderName, value: &'static str| {
        out.insert(name, HeaderValue::from_static(value));
    };
    set(out, header::CONTENT_TYPE, content_type(path));
    set(
        out,
        header::CACHE_CONTROL,
        if immutable {
            "public, max-age=31536000, immutable"
        } else {
            "no-cache"
        },
    );
    set(out, header::X_CONTENT_TYPE_OPTIONS, "nosniff");
    set(out, header::REFERRER_POLICY, "same-origin");
    set(
        out,
        header::CONTENT_SECURITY_POLICY,
        "frame-ancestors https://web.telegram.org",
    );
}

async fn read(path: &Path) -> Option<Vec<u8>> {
    let meta = tokio::fs::metadata(path).await.ok()?;
    if !meta.is_file() || meta.len() > MAX_FILE {
        return None;
    }
    tokio::fs::read(path).await.ok()
}

/// The cabinet's answer to a request the API did not take.
pub async fn respond(root: &Path, method: &Method, path: &str) -> Response {
    let mut response = Response::new(Body::empty());
    if method != Method::GET && method != Method::HEAD {
        *response.status_mut() = StatusCode::METHOD_NOT_ALLOWED;
        response
            .headers_mut()
            .insert(header::ALLOW, HeaderValue::from_static("GET, HEAD"));
        return response;
    }
    let asset = resolve(root, path);
    let (file, body) = match &asset {
        Some(file) => match read(file).await {
            Some(body) => (file.clone(), Some(body)),
            None => (root.join("index.html"), None),
        },
        None => (root.join("index.html"), None),
    };
    // A missing asset is a missing file, not the app: a stale page asking
    // for an old chunk must fail, not get HTML.
    let asset_path = path.trim_start_matches('/').starts_with("_app/");
    let body = match body {
        Some(body) => body,
        None if asset_path => {
            *response.status_mut() = StatusCode::NOT_FOUND;
            return response;
        }
        None => match read(&file).await {
            Some(body) => body,
            None => {
                *response.status_mut() = StatusCode::NOT_FOUND;
                return response;
            }
        },
    };
    let immutable = path.trim_start_matches('/').starts_with("_app/immutable/");
    headers(response.headers_mut(), &file, immutable);
    if method != Method::HEAD {
        *response.body_mut() = Body::from(body);
    }
    response
}

#[cfg(test)]
mod tests {
    use super::*;
    use http_body_util::BodyExt;

    fn root() -> PathBuf {
        let dir = std::env::temp_dir().join(format!("cabinet-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(dir.join("_app/immutable")).unwrap();
        std::fs::write(dir.join("index.html"), "<html>app</html>").unwrap();
        std::fs::write(dir.join("_app/immutable/a.js"), "js").unwrap();
        std::fs::write(dir.join("favicon.svg"), "<svg/>").unwrap();
        dir
    }

    async fn get(root: &Path, path: &str) -> (StatusCode, HeaderMap, String) {
        let response = respond(root, &Method::GET, path).await;
        let status = response.status();
        let headers = response.headers().clone();
        let body = response.into_body().collect().await.unwrap().to_bytes();
        (status, headers, String::from_utf8_lossy(&body).into())
    }

    #[tokio::test]
    async fn files_and_the_app_for_other_paths() {
        let root = root();
        let (status, headers, body) = get(&root, "/_app/immutable/a.js").await;
        assert_eq!((status, body.as_str()), (StatusCode::OK, "js"));
        assert_eq!(
            headers[header::CACHE_CONTROL],
            "public, max-age=31536000, immutable"
        );
        assert!(
            headers[header::CONTENT_TYPE]
                .to_str()
                .unwrap()
                .starts_with("text/javascript")
        );
        let (status, headers, body) = get(&root, "/admin/nodes").await;
        assert_eq!(
            (status, body.as_str()),
            (StatusCode::OK, "<html>app</html>")
        );
        assert_eq!(headers[header::CACHE_CONTROL], "no-cache");
        assert_eq!(
            headers[header::CONTENT_SECURITY_POLICY],
            "frame-ancestors https://web.telegram.org"
        );
        let (_, headers, body) = get(&root, "/favicon.svg").await;
        assert_eq!(
            (
                headers[header::CONTENT_TYPE].to_str().unwrap(),
                body.as_str()
            ),
            ("image/svg+xml", "<svg/>")
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn a_missing_asset_is_not_the_app() {
        let root = root();
        assert_eq!(
            get(&root, "/_app/immutable/old.js").await.0,
            StatusCode::NOT_FOUND
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn no_way_out_of_the_directory() {
        let root = root();
        let secret = root.with_extension("secret");
        std::fs::write(&secret, "secret").unwrap();
        let name = secret.file_name().unwrap().to_str().unwrap().to_string();
        let (_, _, body) = get(&root, &format!("/../{name}")).await;
        assert_eq!(body, "<html>app</html>");
        assert!(resolve(&root, "/a/../../b").is_none());
        std::fs::remove_dir_all(root).unwrap();
        std::fs::remove_file(secret).unwrap();
    }

    #[tokio::test]
    async fn only_reads() {
        let root = root();
        let response = respond(&root, &Method::POST, "/").await;
        assert_eq!(response.status(), StatusCode::METHOD_NOT_ALLOWED);
        let response = respond(&root, &Method::HEAD, "/").await;
        assert_eq!(response.status(), StatusCode::OK);
        assert!(
            response
                .into_body()
                .collect()
                .await
                .unwrap()
                .to_bytes()
                .is_empty()
        );
        std::fs::remove_dir_all(root).unwrap();
    }
}
