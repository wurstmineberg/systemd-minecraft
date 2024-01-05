use {
    serde::Deserialize,
    url::Url,
    crate::VersionSpec,
};

#[derive(Deserialize)]
struct VersionManifestLatest {
    release: String,
    snapshot: String,
}

#[derive(Deserialize)]
pub(crate) struct VersionManifestInfo {
    pub(crate) id: String,
    pub(crate) url: Url,
}

/// <https://launchermeta.mojang.com/mc/game/version_manifest.json>
#[derive(Deserialize)]
pub(crate) struct VersionManifest {
    latest: VersionManifestLatest,
    versions: Vec<VersionManifestInfo>,
}

impl VersionManifest {
    pub(crate) fn get(&self, spec: VersionSpec) -> Option<&VersionManifestInfo> {
        let wanted_ver = match spec {
            VersionSpec::Exact(ref ver) => ver,
            VersionSpec::LatestRelease => &self.latest.release,
            VersionSpec::LatestSnapshot => &self.latest.snapshot,
        };
        self.versions.iter().find(|iter_ver| iter_ver.id == *wanted_ver)
    }
}

#[derive(Deserialize)]
pub(crate) struct VersionInfo {
    pub(crate) downloads: VersionInfoDownloads,
}

#[derive(Deserialize)]
pub(crate) struct VersionInfoDownloads {
    pub(crate) server: VersionInfoDownload,
}

#[derive(Deserialize)]
pub(crate) struct VersionInfoDownload {
    pub(crate) url: Url,
}
