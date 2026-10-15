use std::{
    io::Write,
    process::{Command, Stdio},
};
#[test]
fn cli_accepts_python_fixture() {
    let status = Command::new(env!("CARGO_BIN_EXE_gradia-block-contracts"))
        .arg(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/fixtures/python-events.json"
        ))
        .status()
        .unwrap();
    assert!(status.success());
}
#[test]
fn cli_rejects_bad_duplicate_and_oversized_inputs() {
    for input in [
        b"{}".to_vec(),
        b"{\"privacy\":\"raw\",\"privacy\":\"metadata_only\"}".to_vec(),
        vec![b' '; 1_048_577],
    ] {
        let mut child = Command::new(env!("CARGO_BIN_EXE_gradia-block-contracts"))
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        child.stdin.take().unwrap().write_all(&input).unwrap();
        assert!(!child.wait().unwrap().success());
    }
}
