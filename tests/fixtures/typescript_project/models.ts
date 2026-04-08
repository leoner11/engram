/** Represents a user in the system. */
export interface User {
  id: string;
  name: string;
  email: string;
  role: UserRole;
}

export enum UserRole {
  Admin = "admin",
  Member = "member",
  Guest = "guest",
}

/** User preferences configuration. */
export type UserPrefs = {
  theme: "light" | "dark";
  notifications: boolean;
};
