ui = true
disable_mlock = true
storage "raft" {
  path = "/openbao/file"
  node_id = "ace-local"
}
listener "tcp" {
  address = "0.0.0.0:8200"
  tls_disable = 1
}
api_addr = "http://vault:8200"
cluster_addr = "http://vault:8201"
