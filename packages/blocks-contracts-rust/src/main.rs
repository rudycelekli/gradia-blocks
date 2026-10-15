mod input;
use std::{
    env,
    fs::File,
    io::{self, Read},
    process::ExitCode,
};
fn run() -> Result<usize, String> {
    let args: Vec<_> = env::args().skip(1).collect();
    if args.len() > 1 {
        return Err("usage: gradia-block-contracts [FILE|-]".into());
    }
    let source = args.first().map(String::as_str).unwrap_or("-");
    let mut reader: Box<dyn Read> = if source == "-" {
        Box::new(io::stdin())
    } else {
        Box::new(File::open(source).map_err(|_| "cannot open input file")?)
    };
    let mut bytes = Vec::new();
    reader
        .by_ref()
        .take(1_048_577)
        .read_to_end(&mut bytes)
        .map_err(|_| "cannot read input")?;
    if bytes.len() > 1_048_576 {
        return Err("input exceeds 1 MiB".into());
    }
    let input::Unique(value) =
        serde_json::from_slice(&bytes).map_err(|_| "invalid JSON or duplicate keys")?;
    gradia_block_contracts::validate_document(&value)
}
fn main() -> ExitCode {
    match run() {
        Ok(count) => {
            println!("{{\"valid\":true,\"events\":{count}}}");
            ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!("{}", serde_json::json!({"valid":false,"error":error}));
            ExitCode::FAILURE
        }
    }
}
