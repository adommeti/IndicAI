package main
import ("net/http"; "os"; "time")
func main() {
  c := http.Client{Timeout: 4*time.Second}
  r, err := c.Get(os.Args[1])
  if err != nil { os.Exit(1) }
  defer r.Body.Close()
  if r.StatusCode < 200 || r.StatusCode >= 400 { os.Exit(1) }
}
