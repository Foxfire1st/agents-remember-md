use repository_profile_rust_fixture::add;

#[test]
fn clean_room_service_flow_composes_repository_behavior() {
    let response = format!("{{\"total\":{}}}", add(20, 22));
    assert_eq!(response, "{\"total\":42}");
}
