use anyhow::{anyhow, bail, Context, Result};
use chrono::{SecondsFormat, Utc};
use clap::{Parser, ValueEnum};
use rand::Rng;
use reqwest::{blocking::Client, header::RETRY_AFTER, StatusCode};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::thread::sleep;
use std::time::{Duration, Instant};
use unicode_normalization::{char::is_combining_mark, UnicodeNormalization};

const DBLP_API: &str = "https://dblp.org/search/publ/api";
const FIELD_ORDER: &[&str] = &[
    "author",
    "title",
    "journal",
    "volume",
    "number",
    "pages",
    "year",
    "url",
    "doi",
    "timestamp",
    "biburl",
    "bibsource",
];

#[derive(Parser, Debug)]
#[command(
    name = "theorybib-dblp",
    about = "Generate CryptoBib-style theory BibTeX files from the DBLP JSON API."
)]
struct Args {
    #[arg(long, default_value = ".")]
    root: PathBuf,

    #[arg(long, default_value = "bib")]
    bib_dir: String,

    #[arg(long, default_value = "meta")]
    meta_dir: String,

    #[arg(long, default_value = ".cache/dblp")]
    cache_dir: String,

    #[arg(long, default_value = "venues.json")]
    config: String,

    #[arg(long)]
    write_default_config: bool,

    #[arg(long, action = clap::ArgAction::Append)]
    only: Vec<String>,

    #[arg(long, default_value = "")]
    contact: String,

    /// Minimum seconds between DBLP requests.
    #[arg(long, default_value_t = 8.0)]
    delay: f64,

    /// Extra random seconds added to each DBLP delay.
    #[arg(long, default_value_t = 2.0)]
    jitter: f64,

    /// Seconds to wait after HTTP 429 when Retry-After is absent.
    #[arg(long, default_value_t = 900.0)]
    cooldown: f64,

    #[arg(long, default_value_t = 120.0)]
    timeout: f64,

    #[arg(long, default_value_t = 12)]
    max_retries: u32,

    #[arg(long, default_value_t = 1000)]
    page_size: usize,

    #[arg(long, value_enum, default_value = "refresh")]
    cache_policy: CachePolicy,

    #[arg(long)]
    dry_run: bool,

    #[arg(long)]
    git_commit: bool,

    #[arg(long)]
    git_push: bool,

    // Compatibility flags accepted by the old Python generator.
    #[arg(long, hide = true)]
    json_only: bool,

    #[arg(long, hide = true)]
    individual_fallback: bool,

    #[arg(long, hide = true, default_value = "bib1")]
    bib_format: String,
}

#[derive(Debug, Clone, Copy, ValueEnum, PartialEq, Eq)]
enum CachePolicy {
    Refresh,
    Reuse,
    Offline,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
struct RawVenue {
    id: String,
    label: String,
    stream: String,
    #[serde(default)]
    journal: Option<String>,
    #[serde(default)]
    out: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
struct Venue {
    id: String,
    label: String,
    stream: String,
    journal: String,
    out: String,
}

#[derive(Debug, Clone)]
struct Entry {
    venue: Venue,
    dblp_key: String,
    entry_type: String,
    fields: BTreeMap<String, String>,
    authors: Vec<String>,
    year: String,
    author_label: String,
    base_key: String,
    custom_key: String,
}

#[derive(Debug)]
struct Page {
    total: usize,
    sent: usize,
    first: usize,
    infos: Vec<Value>,
}

struct Cache {
    root: PathBuf,
    policy: CachePolicy,
}

impl Cache {
    fn path(&self, kind: &str, venue: &Venue, offset: usize) -> PathBuf {
        self.root
            .join(kind)
            .join(&venue.id)
            .join(format!("{offset:07}.{kind}"))
    }

    fn get_or_fetch<F>(&self, kind: &str, venue: &Venue, offset: usize, fetch: F) -> Result<String>
    where
        F: FnOnce() -> Result<String>,
    {
        let path = self.path(kind, venue, offset);
        if matches!(self.policy, CachePolicy::Reuse | CachePolicy::Offline) && path.exists() {
            return fs::read_to_string(&path)
                .with_context(|| format!("reading cache file {}", path.display()));
        }
        if self.policy == CachePolicy::Offline {
            bail!("missing cache file in offline mode: {}", path.display());
        }
        let text = fetch()?;
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)
                .with_context(|| format!("creating cache directory {}", parent.display()))?;
        }
        fs::write(&path, &text).with_context(|| format!("writing cache file {}", path.display()))?;
        Ok(text)
    }
}

struct Fetcher {
    client: Client,
    delay: f64,
    jitter: f64,
    cooldown: f64,
    max_retries: u32,
    last_request: Option<Instant>,
}

impl Fetcher {
    fn new(args: &Args) -> Result<Self> {
        let contact = if args.contact.trim().is_empty() {
            std::env::var("THEORYBIB_CONTACT").unwrap_or_else(|_| "contact=unset".to_string())
        } else {
            args.contact.trim().to_string()
        };
        let user_agent = format!("theorybib-dblp/0.1 ({contact}; polite DBLP JSON API client)");
        let client = Client::builder()
            .user_agent(user_agent)
            .timeout(Duration::from_secs_f64(args.timeout.max(1.0)))
            .redirect(reqwest::redirect::Policy::limited(10))
            .build()
            .context("building HTTP client")?;
        Ok(Self {
            client,
            delay: args.delay.max(0.0),
            jitter: args.jitter.max(0.0),
            cooldown: args.cooldown.max(1.0),
            max_retries: args.max_retries,
            last_request: None,
        })
    }

    fn throttle(&mut self) {
        let jitter = if self.jitter > 0.0 {
            rand::thread_rng().gen_range(0.0..=self.jitter)
        } else {
            0.0
        };
        let target = Duration::from_secs_f64(self.delay + jitter);
        if let Some(last) = self.last_request {
            let elapsed = last.elapsed();
            if elapsed < target {
                sleep(target - elapsed);
            }
        }
        self.last_request = Some(Instant::now());
    }

    fn get(&mut self, url: &str, params: &[(String, String)]) -> Result<String> {
        let mut last_error: Option<anyhow::Error> = None;

        for attempt in 0..=self.max_retries {
            self.throttle();
            let response = self.client.get(url).query(params).send();

            match response {
                Ok(resp) => {
                    let status = resp.status();
                    if status.is_success() {
                        return resp.text().context("reading HTTP response body");
                    }

                    let retry_after = parse_retry_after(resp.headers())
                        .unwrap_or_else(|| self.default_backoff(status, attempt));
                    let body = resp.text().unwrap_or_default();

                    if is_retryable_status(status) && attempt < self.max_retries {
                        eprintln!(
                            "HTTP {status} from DBLP; retry {}/{} after {:.0}s. sample={:?}",
                            attempt + 1,
                            self.max_retries,
                            retry_after.as_secs_f64(),
                            one_line(&body, 160)
                        );
                        sleep(retry_after);
                        continue;
                    }

                    bail!(
                        "HTTP {status} for {url} with params {:?}: {}",
                        params,
                        one_line(&body, 500)
                    );
                }
                Err(err) => {
                    if attempt < self.max_retries {
                        let delay = Duration::from_secs_f64((20.0 * 2f64.powi(attempt as i32)).min(300.0));
                        eprintln!(
                            "network error from DBLP; retry {}/{} after {:.0}s: {}",
                            attempt + 1,
                            self.max_retries,
                            delay.as_secs_f64(),
                            err
                        );
                        sleep(delay);
                        continue;
                    }
                    last_error = Some(anyhow!(err));
                }
            }
        }

        Err(last_error.unwrap_or_else(|| anyhow!("request failed after retries")))
    }

    fn default_backoff(&self, status: StatusCode, attempt: u32) -> Duration {
        if status == StatusCode::TOO_MANY_REQUESTS {
            Duration::from_secs_f64(self.cooldown * f64::from(attempt + 1))
        } else {
            Duration::from_secs_f64((10.0 * 2f64.powi(attempt as i32)).min(300.0))
        }
    }
}

fn main() -> Result<()> {
    let args = Args::parse();
    let root = args.root.canonicalize().unwrap_or_else(|_| args.root.clone());
    let config_path = root.join(&args.config);

    if args.write_default_config {
        write_default_config(&config_path)?;
        return Ok(());
    }

    let mut venues = load_venues(&config_path)?;
    if !args.only.is_empty() {
        let selected: BTreeSet<String> = args.only.iter().map(|x| x.to_lowercase()).collect();
        venues.retain(|v| selected.contains(&v.id.to_lowercase()) || selected.contains(&v.label.to_lowercase()));
        if venues.is_empty() {
            bail!("--only selected no venues: {:?}", selected);
        }
    }

    let bib_dir = root.join(&args.bib_dir);
    let meta_dir = root.join(&args.meta_dir);
    let cache = Cache {
        root: root.join(&args.cache_dir),
        policy: args.cache_policy,
    };
    let mut fetcher = Fetcher::new(&args)?;
    let page_size = args.page_size.clamp(1, 1000);

    fs::create_dir_all(&bib_dir).with_context(|| format!("creating {}", bib_dir.display()))?;
    fs::create_dir_all(&meta_dir).with_context(|| format!("creating {}", meta_dir.display()))?;

    let mut all_entries = Vec::new();
    for venue in &venues {
        let mut entries = fetch_venue(venue.clone(), &mut fetcher, &cache, page_size)?;
        all_entries.append(&mut entries);
    }

    for entry in &mut all_entries {
        derive_key_material(entry);
    }

    let keymap_path = meta_dir.join("keymap.tsv");
    let old_keymap = read_keymap(&keymap_path)?;
    assign_custom_keys(&mut all_entries, &old_keymap);

    if args.dry_run {
        eprintln!("dry run: fetched {} entries across {} venues", all_entries.len(), venues.len());
        return Ok(());
    }

    write_outputs(&bib_dir, &meta_dir, &keymap_path, &venues, &all_entries, &args)?;

    if args.git_commit || args.git_push {
        git_commit_and_maybe_push(&root, args.git_push)?;
    }

    Ok(())
}

fn write_default_config(path: &Path) -> Result<()> {
    if path.exists() {
        bail!("refusing to overwrite existing {}", path.display());
    }
    let defaults = default_venues();
    let text = serde_json::to_string_pretty(&defaults)? + "\n";
    fs::write(path, text).with_context(|| format!("writing {}", path.display()))?;
    eprintln!("wrote {}", path.display());
    Ok(())
}

fn default_venues() -> Vec<Venue> {
    vec![
        Venue { id: "jsl".into(), label: "JSL".into(), stream: "journals/jsyml".into(), journal: "Journal of Symbolic Logic".into(), out: "jsl.bib".into() },
        Venue { id: "tcs".into(), label: "TCS".into(), stream: "journals/tcs".into(), journal: "Theoretical Computer Science".into(), out: "tcs.bib".into() },
        Venue { id: "combinatorica".into(), label: "COMB".into(), stream: "journals/combinatorica".into(), journal: "Combinatorica".into(), out: "combinatorica.bib".into() },
        Venue { id: "ipl".into(), label: "IPL".into(), stream: "journals/ipl".into(), journal: "Information Processing Letters".into(), out: "ipl.bib".into() },
        Venue { id: "jacm".into(), label: "JACM".into(), stream: "journals/jacm".into(), journal: "Journal of the ACM".into(), out: "jacm.bib".into() },
        Venue { id: "ecc".into(), label: "ECC".into(), stream: "journals/eccc".into(), journal: "Electronic Colloquium on Computational Complexity".into(), out: "ecc.bib".into() },
    ]
}

fn load_venues(path: &Path) -> Result<Vec<Venue>> {
    if !path.exists() {
        return Ok(default_venues());
    }
    let text = fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
    let raw: Vec<RawVenue> = serde_json::from_str(&text).with_context(|| format!("parsing {}", path.display()))?;
    let mut venues = Vec::new();
    let mut seen = BTreeSet::new();
    for item in raw {
        let id = item.id.trim().to_string();
        let label = item.label.trim().to_string();
        let stream = normalize_stream(&item.stream);
        if id.is_empty() || label.is_empty() || stream.is_empty() {
            bail!("bad venue item in {}", path.display());
        }
        if !seen.insert(id.clone()) {
            bail!("duplicate venue id: {id}");
        }
        let journal = item.journal.unwrap_or_else(|| label.clone());
        let out = item.out.unwrap_or_else(|| format!("{id}.bib"));
        venues.push(Venue { id, label, stream, journal, out });
    }
    Ok(venues)
}

fn normalize_stream(stream: &str) -> String {
    let s = stream.trim().trim_end_matches(':');
    s.strip_prefix("streams/").unwrap_or(s).to_string()
}

fn fetch_venue(venue: Venue, fetcher: &mut Fetcher, cache: &Cache, page_size: usize) -> Result<Vec<Entry>> {
    let query = format!("streamid:{}:", venue.stream);
    let mut entries = Vec::new();
    let mut seen = BTreeSet::new();
    let mut offset = 0usize;
    let mut total_seen = None;

    eprintln!("\n== {} [{}] ==", venue.label, venue.stream);

    loop {
        let text = cache.get_or_fetch("json", &venue, offset, || {
            fetcher.get(
                DBLP_API,
                &[
                    ("q".into(), query.clone()),
                    ("format".into(), "json".into()),
                    ("h".into(), page_size.to_string()),
                    ("f".into(), offset.to_string()),
                    ("c".into(), "0".into()),
                ],
            )
        })?;
        let page = parse_json_page(&text)?;
        if total_seen.is_none() {
            total_seen = Some(page.total);
            eprintln!("DBLP reports {} records", page.total);
        }
        eprintln!(
            "[{}] page first={} sent={} progress={}/{}",
            venue.id,
            page.first,
            page.sent,
            page.first + page.sent,
            page.total
        );
        if page.sent == 0 || page.infos.is_empty() {
            break;
        }
        for info in page.infos {
            let entry = entry_from_json_info(&venue, &info)?;
            if seen.insert(entry.dblp_key.clone()) {
                entries.push(entry);
            } else {
                eprintln!("warning: duplicate DBLP key in {}: {}", venue.id, entry.dblp_key);
            }
        }
        offset = page.first + page.sent;
        if offset >= page.total {
            break;
        }
    }

    if let Some(total) = total_seen {
        if entries.len() != total {
            eprintln!(
                "warning: collected {} entries but DBLP reported {} for {}",
                entries.len(),
                total,
                venue.id
            );
        }
    }
    eprintln!("collected {} entries for {}", entries.len(), venue.id);
    Ok(entries)
}

fn parse_json_page(text: &str) -> Result<Page> {
    let data: Value = serde_json::from_str(text).context("parsing DBLP JSON")?;
    let hits = data
        .get("result")
        .and_then(|v| v.get("hits"))
        .ok_or_else(|| anyhow!("DBLP JSON missing result.hits"))?;
    let total = attr_usize(hits, "@total")?;
    let sent = attr_usize(hits, "@sent")?;
    let first = attr_usize(hits, "@first")?;
    let mut infos = Vec::new();
    match hits.get("hit") {
        Some(Value::Array(items)) => {
            for hit in items {
                if let Some(info) = hit.get("info") {
                    infos.push(info.clone());
                }
            }
        }
        Some(Value::Object(_)) => {
            if let Some(info) = hits.get("hit").and_then(|h| h.get("info")) {
                infos.push(info.clone());
            }
        }
        _ => {}
    }
    Ok(Page { total, sent, first, infos })
}

fn attr_usize(obj: &Value, name: &str) -> Result<usize> {
    let v = obj.get(name).ok_or_else(|| anyhow!("missing DBLP attribute {name}"))?;
    if let Some(s) = v.as_str() {
        return s.parse::<usize>().with_context(|| format!("parsing {name}={s:?}"));
    }
    if let Some(n) = v.as_u64() {
        return usize::try_from(n).with_context(|| format!("converting {name}={n}"));
    }
    bail!("unexpected type for {name}: {v}");
}

fn entry_from_json_info(venue: &Venue, info: &Value) -> Result<Entry> {
    let dblp_key = value_str(info.get("key")).ok_or_else(|| anyhow!("DBLP info lacks key"))?;
    let authors = authors_from_json(info);
    let mut fields: BTreeMap<String, String> = BTreeMap::new();

    if !authors.is_empty() {
        fields.insert("author".into(), authors.join(" and "));
    }
    if let Some(title) = value_str(info.get("title")) {
        let title = normalize_title(&title);
        if !title.is_empty() {
            fields.insert("title".into(), title);
        }
    }
    let journal = value_str(info.get("venue")).unwrap_or_else(|| venue.journal.clone());
    fields.insert("journal".into(), normalize_space(&journal));

    for (src, dst) in [
        ("volume", "volume"),
        ("number", "number"),
        ("pages", "pages"),
        ("year", "year"),
        ("doi", "doi"),
    ] {
        if let Some(v) = value_str(info.get(src)) {
            let v = normalize_space(&v);
            if !v.is_empty() {
                fields.insert(dst.into(), v);
            }
        }
    }

    if let Some(ee) = first_value_str(info.get("ee")) {
        fields.insert("url".into(), ee);
    } else if let Some(url) = value_str(info.get("url")) {
        let url = if url.starts_with("http://") || url.starts_with("https://") {
            url
        } else {
            format!("https://dblp.org/{}", url.trim_start_matches('/'))
        };
        fields.insert("url".into(), url);
    }

    fields.insert("biburl".into(), format!("https://dblp.org/rec/{dblp_key}.bib"));
    fields.insert(
        "bibsource".into(),
        "dblp computer science bibliography, https://dblp.org".into(),
    );

    let year = fields.get("year").cloned().unwrap_or_default();

    Ok(Entry {
        venue: venue.clone(),
        dblp_key,
        entry_type: "article".into(),
        fields,
        authors,
        year,
        author_label: String::new(),
        base_key: String::new(),
        custom_key: String::new(),
    })
}

fn authors_from_json(info: &Value) -> Vec<String> {
    let Some(author_value) = info
        .get("authors")
        .and_then(|v| v.get("author"))
    else {
        return Vec::new();
    };
    match author_value {
        Value::Array(items) => items.iter().filter_map(|v| value_str(Some(v))).collect(),
        Value::Object(_) | Value::String(_) => value_str(Some(author_value)).into_iter().collect(),
        _ => Vec::new(),
    }
}

fn value_str(v: Option<&Value>) -> Option<String> {
    match v? {
        Value::String(s) => Some(s.trim().to_string()),
        Value::Number(n) => Some(n.to_string()),
        Value::Object(map) => map
            .get("text")
            .and_then(|x| x.as_str())
            .map(|s| s.trim().to_string()),
        _ => None,
    }
}

fn first_value_str(v: Option<&Value>) -> Option<String> {
    match v? {
        Value::Array(items) => items.iter().find_map(|x| value_str(Some(x))),
        _ => value_str(v),
    }
}

fn normalize_space(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn normalize_title(s: &str) -> String {
    normalize_space(s).trim_end_matches('.').trim().to_string()
}

fn derive_key_material(entry: &mut Entry) {
    entry.year = normalize_year(entry.fields.get("year").map(|s| s.as_str()).unwrap_or(""));
    entry.author_label = author_label(&entry.authors);
    let yy = year_suffix(&entry.year);
    entry.base_key = format!("{}:{}{}", entry.venue.label, entry.author_label, yy);
}

fn normalize_year(year: &str) -> String {
    let digits: String = year.chars().filter(|c| c.is_ascii_digit()).collect();
    if digits.len() >= 4 {
        digits[0..4].to_string()
    } else {
        year.trim().to_string()
    }
}

fn year_suffix(year: &str) -> String {
    let digits: String = year.chars().filter(|c| c.is_ascii_digit()).collect();
    if digits.len() >= 2 {
        digits[digits.len() - 2..].to_string()
    } else {
        "XX".into()
    }
}

fn author_label(authors: &[String]) -> String {
    let last_names: Vec<String> = authors.iter().map(|a| clean_last_name(a)).collect();
    if last_names.is_empty() {
        return "Anon".into();
    }
    if last_names.len() == 1 {
        return capitalize_preserving(&last_names[0]);
    }
    if last_names.len() <= 3 {
        return last_names.iter().map(|s| abbrev(s, 3)).collect::<Vec<_>>().join("");
    }
    let mut out = String::new();
    for name in last_names.iter().take(6) {
        let ch = name.chars().find(|c| c.is_ascii_alphanumeric()).unwrap_or('X');
        out.push(ch.to_ascii_uppercase());
    }
    if out.is_empty() { "Anon".into() } else { out }
}

fn clean_last_name(author: &str) -> String {
    let mut parts: Vec<&str> = author.split_whitespace().collect();
    if parts.last().is_some_and(|x| x.len() == 4 && x.chars().all(|c| c.is_ascii_digit())) {
        parts.pop();
    }
    let raw = parts.last().copied().unwrap_or(author);
    let ascii: String = raw
        .nfd()
        .filter(|c| !is_combining_mark(*c))
        .collect::<String>()
        .chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .collect();
    if ascii.is_empty() { "X".into() } else { ascii }
}

fn capitalize_preserving(s: &str) -> String {
    let mut chars = s.chars();
    match chars.next() {
        Some(first) => format!("{}{}", first.to_ascii_uppercase(), chars.collect::<String>()),
        None => "X".into(),
    }
}

fn abbrev(s: &str, n: usize) -> String {
    let raw: String = s.chars().filter(|c| c.is_ascii_alphanumeric()).take(n).collect();
    if raw.is_empty() {
        return "X".into();
    }
    let mut chars = raw.chars();
    let first = chars.next().unwrap().to_ascii_uppercase();
    let rest: String = chars.map(|c| c.to_ascii_lowercase()).collect();
    format!("{first}{rest}")
}

fn read_keymap(path: &Path) -> Result<HashMap<String, String>> {
    if !path.exists() {
        return Ok(HashMap::new());
    }
    let text = fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
    let mut result = HashMap::new();
    for line in text.lines() {
        if line.trim().is_empty() || line.starts_with('#') {
            continue;
        }
        let parts: Vec<&str> = line.split('\t').collect();
        if parts.len() < 2 || parts[0] == "dblp_key" {
            continue;
        }
        if !parts[0].is_empty() && !parts[1].is_empty() {
            result.insert(parts[0].to_string(), parts[1].to_string());
        }
    }
    Ok(result)
}

fn assign_custom_keys(entries: &mut [Entry], old: &HashMap<String, String>) {
    let mut used: BTreeMap<String, String> = BTreeMap::new();
    let mut unmapped = Vec::new();

    let mut order: Vec<usize> = (0..entries.len()).collect();
    order.sort_by(|&a, &b| entries[a].dblp_key.cmp(&entries[b].dblp_key));

    for i in order {
        if let Some(old_key) = old.get(&entries[i].dblp_key) {
            if !used.contains_key(old_key) {
                entries[i].custom_key = old_key.clone();
                used.insert(old_key.clone(), entries[i].dblp_key.clone());
                continue;
            }
        }
        unmapped.push(i);
    }

    let mut groups: BTreeMap<String, Vec<usize>> = BTreeMap::new();
    for i in unmapped {
        groups.entry(entries[i].base_key.clone()).or_default().push(i);
    }

    for (base, mut group) in groups {
        group.sort_by(|&a, &b| {
            (&entries[a].year, &entries[a].author_label, &entries[a].dblp_key)
                .cmp(&(&entries[b].year, &entries[b].author_label, &entries[b].dblp_key))
        });
        let clean_collision = group.len() > 1
            && !used.contains_key(&base)
            && !used.keys().any(|k| k.starts_with(&base));

        for (idx, &i) in group.iter().enumerate() {
            let key = if group.len() == 1 && !used.contains_key(&base) {
                base.clone()
            } else if clean_collision {
                format!("{}{}", base, suffix(idx))
            } else {
                first_free_key(&base, &used)
            };
            entries[i].custom_key = key.clone();
            used.insert(key, entries[i].dblp_key.clone());
        }
    }
}

fn suffix(mut i: usize) -> String {
    let mut letters = Vec::new();
    i += 1;
    while i > 0 {
        i -= 1;
        letters.push((b'a' + (i % 26) as u8) as char);
        i /= 26;
    }
    letters.iter().rev().collect()
}

fn first_free_key(base: &str, used: &BTreeMap<String, String>) -> String {
    if !used.contains_key(base) {
        return base.to_string();
    }
    for i in 0usize.. {
        let key = format!("{}{}", base, suffix(i));
        if !used.contains_key(&key) {
            return key;
        }
    }
    unreachable!()
}

fn write_outputs(
    bib_dir: &Path,
    meta_dir: &Path,
    keymap_path: &Path,
    venues: &[Venue],
    entries: &[Entry],
    args: &Args,
) -> Result<()> {
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true);

    for venue in venues {
        let mut venue_entries: Vec<&Entry> = entries.iter().filter(|e| e.venue.id == venue.id).collect();
        venue_entries.sort_by(|a, b| a.custom_key.cmp(&b.custom_key));
        let text = bib_file_text(Some(venue), &venue_entries, &now);
        let out_path = bib_dir.join(&venue.out);
        fs::write(&out_path, text).with_context(|| format!("writing {}", out_path.display()))?;
        eprintln!("wrote {} entries -> {}", venue_entries.len(), out_path.display());
    }

    let mut all_entries: Vec<&Entry> = entries.iter().collect();
    all_entries.sort_by(|a, b| a.custom_key.cmp(&b.custom_key));
    fs::write(bib_dir.join("all.bib"), bib_file_text(None, &all_entries, &now))
        .with_context(|| format!("writing {}", bib_dir.join("all.bib").display()))?;

    write_keymap(keymap_path, entries)?;
    write_manifest(&meta_dir.join("manifest.json"), venues, entries, args, &now)?;
    Ok(())
}

fn bib_file_text(venue: Option<&Venue>, entries: &[&Entry], now: &str) -> String {
    let (title, label) = if let Some(v) = venue {
        (format!("{} bibliography", v.journal), v.label.clone())
    } else {
        ("combined theory bibliography".into(), "ALL".into())
    };
    let mut out = format!(
        "% File generated by theorybib-dblp -- DO NOT EDIT MANUALLY.\n\
         % Generated at: {now}\n\
         % Source: DBLP publication search API, JSON format.\n\
         % Bibliography: {title}\n\
         %\n\
         % Labeling convention, following the CryptoBib style:\n\
         %   venue-label ':' author-label two-digit-year [collision-letter]\n\
         %   one author: full last name, e.g. {label}:Shamir79\n\
         %   two/three authors: first three letters of each last name, e.g. {label}:AliBob98\n\
         %   four or more authors: first letters of up to six last names, e.g. {label}:ABCDEG98\n\
         % Existing keys are preserved via meta/keymap.tsv.\n\n"
    );
    for entry in entries {
        out.push_str(&format_bib_entry(entry));
        out.push_str("\n\n");
    }
    out
}

fn format_bib_entry(entry: &Entry) -> String {
    let mut out = format!("@{}{{{},\n", entry.entry_type, entry.custom_key);
    let mut written = BTreeSet::new();
    for &name in FIELD_ORDER {
        if let Some(value) = entry.fields.get(name) {
            if !value.is_empty() {
                out.push_str(&format!("  {name:<10} = {{{}}},\n", escape_bib_value(name, value)));
                written.insert(name.to_string());
            }
        }
    }
    for (name, value) in &entry.fields {
        if !written.contains(name) && !value.is_empty() {
            out.push_str(&format!("  {name:<10} = {{{}}},\n", escape_bib_value(name, value)));
        }
    }
    if out.ends_with(",\n") {
        out.truncate(out.len() - 2);
        out.push('\n');
    }
    out.push('}');
    out
}

fn escape_bib_value(field: &str, value: &str) -> String {
    if matches!(field, "url" | "doi" | "biburl" | "bibsource" | "pages" | "volume" | "number" | "year" | "timestamp") {
        return value.to_string();
    }
    let mut out = String::new();
    for ch in value.chars() {
        match ch {
            '&' => out.push_str(r"\&"),
            '%' => out.push_str(r"\%"),
            '#' => out.push_str(r"\#"),
            '_' => out.push_str(r"\_"),
            '$' => out.push_str(r"\$"),
            _ => out.push(ch),
        }
    }
    out
}

fn write_keymap(path: &Path, entries: &[Entry]) -> Result<()> {
    let mut refs: Vec<&Entry> = entries.iter().collect();
    refs.sort_by(|a, b| (&a.venue.id, &a.custom_key, &a.dblp_key).cmp(&(&b.venue.id, &b.custom_key, &b.dblp_key)));
    let mut out = String::from("dblp_key\tcustom_key\tvenue\tyear\tauthor_label\tbase_key\toriginal_bib_key\n");
    for e in refs {
        out.push_str(&format!(
            "{}\t{}\t{}\t{}\t{}\t{}\tDBLP:{}\n",
            e.dblp_key, e.custom_key, e.venue.id, e.year, e.author_label, e.base_key, e.dblp_key
        ));
    }
    fs::write(path, out).with_context(|| format!("writing {}", path.display()))?;
    Ok(())
}

fn write_manifest(path: &Path, venues: &[Venue], entries: &[Entry], args: &Args, now: &str) -> Result<()> {
    let mut counts: BTreeMap<String, usize> = venues.iter().map(|v| (v.id.clone(), 0usize)).collect();
    for e in entries {
        *counts.entry(e.venue.id.clone()).or_default() += 1;
    }
    let manifest = json!({
        "generated_at": now,
        "source": "dblp-json-api-rust",
        "json_only": true,
        "counts": counts,
        "venues": venues,
        "page_size": args.page_size.clamp(1, 1000),
    });
    fs::write(path, serde_json::to_string_pretty(&manifest)? + "\n")
        .with_context(|| format!("writing {}", path.display()))?;
    Ok(())
}

fn git_commit_and_maybe_push(root: &Path, push: bool) -> Result<()> {
    let status = Command::new("git")
        .args(["status", "--porcelain", "bib", "meta"])
        .current_dir(root)
        .output()
        .context("running git status")?;
    if !status.status.success() {
        bail!("git status failed: {}", String::from_utf8_lossy(&status.stderr));
    }
    if status.stdout.is_empty() {
        eprintln!("no bibliography changes");
        return Ok(());
    }
    run_git(root, &["add", "bib", "meta"])?;
    let msg = format!("Update DBLP bibliography {}", Utc::now().format("%Y-%m-%d"));
    run_git(root, &["commit", "-m", &msg])?;
    if push {
        run_git(root, &["push"])?;
    }
    Ok(())
}

fn run_git(root: &Path, args: &[&str]) -> Result<()> {
    let status = Command::new("git")
        .args(args)
        .current_dir(root)
        .status()
        .with_context(|| format!("running git {}", args.join(" ")))?;
    if !status.success() {
        bail!("git {} failed with status {}", args.join(" "), status);
    }
    Ok(())
}

fn parse_retry_after(headers: &reqwest::header::HeaderMap) -> Option<Duration> {
    let raw = headers.get(RETRY_AFTER)?.to_str().ok()?.trim();
    raw.parse::<u64>().ok().map(Duration::from_secs)
}

fn is_retryable_status(status: StatusCode) -> bool {
    status == StatusCode::TOO_MANY_REQUESTS
        || status == StatusCode::REQUEST_TIMEOUT
        || status.is_server_error()
}

fn one_line(s: &str, max_chars: usize) -> String {
    let collapsed = s.split_whitespace().collect::<Vec<_>>().join(" ");
    if collapsed.chars().count() <= max_chars {
        collapsed
    } else {
        format!("{}...", collapsed.chars().take(max_chars).collect::<String>())
    }
}
