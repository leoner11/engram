import { createUser, getUserDisplay, UserService } from "./service";

function main(): void {
  const service = new UserService();
  const user = createUser("Alice", "alice@example.com");
  service.addUser(user);

  const display = getUserDisplay(user);
  console.log(display);

  const found = service.getUser(user.id);
  if (found) {
    console.log(`Found: ${found.name}`);
  }
}

main();
