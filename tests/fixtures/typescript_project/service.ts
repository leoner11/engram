import { User, UserRole, UserPrefs } from "./models";

/** Create a new user with default preferences. */
export function createUser(name: string, email: string): User {
  return {
    id: crypto.randomUUID(),
    name,
    email,
    role: UserRole.Member,
  };
}

/** Get user display name with role badge. */
export const getUserDisplay = (user: User): string => {
  const badge = user.role === UserRole.Admin ? "[Admin]" : "";
  return `${user.name} ${badge}`.trim();
};

/** Validate user email format. */
function validateEmail(email: string): boolean {
  return email.includes("@") && email.includes(".");
}

export class UserService {
  private users: Map<string, User> = new Map();

  /** Add a user to the service. */
  addUser(user: User): void {
    if (!validateEmail(user.email)) {
      throw new Error("Invalid email");
    }
    this.users.set(user.id, user);
  }

  /** Find a user by ID. */
  getUser(id: string): User | undefined {
    return this.users.get(id);
  }

  /** List all admin users. */
  getAdmins(): User[] {
    return Array.from(this.users.values()).filter(
      (u) => u.role === UserRole.Admin
    );
  }
}
