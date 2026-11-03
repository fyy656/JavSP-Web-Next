# kotlinx.serialization
-keepattributes *Annotation*, InnerClasses
-dontnote kotlinx.serialization.**
-keepclassmembers class **$$serializer { *; }
-keepclasseswithmembers class com.fyy656.javspweb.** {
    kotlinx.serialization.KSerializer serializer(...);
}
