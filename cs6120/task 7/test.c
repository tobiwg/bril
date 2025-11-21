int foo(int x) {
  int a = x + 1;
  int b = 5;  // dead
  return a;
}
