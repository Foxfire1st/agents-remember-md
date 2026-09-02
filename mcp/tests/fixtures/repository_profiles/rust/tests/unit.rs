use repository_profile_rust_fixture::add;

#[test]
fn adds_two_values() {
    assert_eq!(add(2, 3), 5);
}
